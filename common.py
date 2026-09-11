# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Kochiya3309
import os
import time
import json
import re
import hashlib
import builtins
from openai import OpenAI
from script_text_io import read_script_text_exact
from output_layout import output_file, prepare_output

# ======================================================================
#  文件日志（通过 monkey-patch builtins.print 实现）
# ======================================================================

_original_print = builtins.print
_log_fp = None

def _logged_print(*args, **kwargs):
    """模块级函数（非闭包），避免被赋给 builtins.print 后
    被反射查找 getattr(common, '_logged_print') 时失败。"""
    _original_print(*args, **kwargs)
    if _log_fp is not None:
        log_kwargs = {k: v for k, v in kwargs.items() if k not in ('file', 'flush')}
        log_kwargs['file'] = _log_fp
        log_kwargs['flush'] = True
        _original_print(*args, **log_kwargs)

def _init_logging():
    global _log_fp
    log_file = os.environ.get("LOG_FILE")
    if log_file and _log_fp is None:
        output_root = os.path.abspath(os.environ.get("OUTPUT_DIR", "./output"))
        canonical_log = output_file(output_root, "pipeline.log")
        if os.path.abspath(log_file) in {os.path.join(output_root, "pipeline.log"), canonical_log}:
            prepare_output(output_root)
            log_file = canonical_log
            os.environ["LOG_FILE"] = log_file
        log_dir = os.path.dirname(os.path.abspath(log_file))
        os.makedirs(log_dir, exist_ok=True)
        _log_fp = open(log_file, "a", encoding="utf-8")
        builtins.print = _logged_print

_init_logging()

# ======================================================================
#  全局搜索缓存
# ======================================================================
_search_cache = {}
_search_cache_hits = 0
_search_cache_misses = 0

def clear_search_cache():
    global _search_cache, _search_cache_hits, _search_cache_misses
    _search_cache = {}
    _search_cache_hits = 0
    _search_cache_misses = 0

def get_search_cache_stats():
    total = _search_cache_hits + _search_cache_misses
    rate = f"{_search_cache_hits}/{total}" if total > 0 else "0/0"
    return f"搜索缓存命中：{rate}（节省 {_search_cache_hits} 次搜索 API 调用）"

# ======================================================================
#  全局 Token 统计
# ======================================================================
_usage = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "cached_tokens": 0,
    "llm_calls": 0,
    "tavily_calls": 0,
    "exa_calls": 0,
}

def reset_usage():
    global _usage
    _usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "llm_calls": 0,
        "tavily_calls": 0,
        "exa_calls": 0,
    }

def _add_usage(u):
    global _usage
    if u is None:
        return
    _usage["prompt_tokens"] += getattr(u, 'prompt_tokens', 0) or 0
    _usage["completion_tokens"] += getattr(u, 'completion_tokens', 0) or 0
    _usage["total_tokens"] += getattr(u, 'total_tokens', 0) or 0
    details = getattr(u, 'prompt_tokens_details', None)
    if details:
        cached = getattr(details, 'cached_tokens', None)
        if cached is not None:
            _usage["cached_tokens"] += cached

def get_usage_report():
    u = _usage
    total = u["total_tokens"]
    if total == 0:
        return "（本次未调用 LLM API）"
    prompt = u["prompt_tokens"]
    cached = u["cached_tokens"]
    missed = prompt - cached
    cache_rate = (cached / prompt * 100) if prompt > 0 else 0
    search_calls = u["tavily_calls"] + u["exa_calls"]
    # V3.6 起不再估算费用（LLM 服务商可换、单价多变），仅统计 token 用量
    lines = [
        "=" * 55,
        "  Token 用量报告",
        "=" * 55,
        f"  LLM 调用：    {u['llm_calls']} 次",
        f"  联网搜索：    {search_calls} 次"
        f"（Tavily {u['tavily_calls']} + Exa {u['exa_calls']}）",
        f"  输入 tokens： {prompt:>10,}",
        f"    缓存命中：  {cached:>10,}（{cache_rate:.1f}%）",
        f"    未缓存：    {missed:>10,}",
        f"  输出 tokens： {u['completion_tokens']:>10,}",
        f"  总计 tokens： {total:>10,}",
    ]
    return "\n".join(lines)

# ======================================================================
#  LLM 配置（V3.6 起：OpenAI 兼容格式，由 .env 的 OPENAI_* 变量配置）
# ======================================================================

_llm_config = None  # 模块级缓存（进程启动时环境变量已注入，运行中不变）

def get_llm_config():
    """读取 LLM 配置（OpenAI 兼容 API）。
    模块级缓存一次：进程启动时环境变量已注入，运行中配置不变，
    缓存可避免每轮 API 调用重复读取、并防止运行中途配置漂移。
    支持任意 OpenAI 兼容服务；OPENAI_API_KEY 为空时回退旧
    DEEPSEEK_API_KEY（平滑迁移旧配置）。
    返回 (api_key, base_url, model, enable_thinking, max_tokens)。"""
    global _llm_config
    if _llm_config is None:
        api_key = (os.environ.get("OPENAI_API_KEY", "").strip()
                   or os.environ.get("DEEPSEEK_API_KEY", "").strip())
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.deepseek.com"
        model = os.environ.get("OPENAI_MODEL", "").strip() or "deepseek-v4-pro"
        enable_thinking = os.environ.get("OPENAI_ENABLE_THINKING", "0") == "1"
        try:
            max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "131072"))
        except ValueError:
            max_tokens = 131072
        _llm_config = (api_key, base_url, model, enable_thinking, max_tokens)
    return _llm_config

def _api_error_hint(e):
    """API 错误提示：max_tokens 超限类错误提示用户调低 OPENAI_MAX_TOKENS。"""
    msg = str(e)
    if "max_tokens" in msg.lower() or "maximum context length" in msg.lower():
        return "（若为 max_tokens 超限，请调低 .env 的 OPENAI_MAX_TOKENS）"
    return ""

def _build_extra_body(budget_tokens):
    """构建 extra_body：仅当启用思维链时传 thinking 参数。
    thinking 是 DeepSeek 的非标准参数，OpenAI 等标准服务会拒绝未知参数，
    因此开关关闭（默认）时不传任何额外参数。"""
    _, _, _, enable_thinking, _ = get_llm_config()
    if enable_thinking:
        return {"thinking": {"type": "enabled", "budget_tokens": budget_tokens}}
    return None

# ======================================================================
#  API 客户端（模块级缓存）
# ======================================================================

_llm_client = None

def get_llm_client():
    """获取 LLM API 客户端（OpenAI 兼容）。
    服务地址与模型由 .env 的 OPENAI_BASE_URL / OPENAI_MODEL 配置，
    未配置时默认 DeepSeek。"""
    global _llm_client
    if _llm_client is None:
        api_key, base_url, _, _, _ = get_llm_config()
        if not api_key:
            print("❌ 未配置 OPENAI_API_KEY（或旧 DEEPSEEK_API_KEY），请在 .env 中配置")
            raise RuntimeError("缺少 LLM API Key")
        _llm_client = OpenAI(api_key=api_key, base_url=base_url)
    return _llm_client

def get_deepseek_client():
    """历史别名，请使用 get_llm_client。"""
    return get_llm_client()

def get_search_keys():
    """返回 (tavily_key, exa_key)。两个 key 都配置时并行查询两家并合并去重，
    只配置一个时仅使用该提供商，均未配置时搜索自动关闭。"""
    return (
        os.environ.get("TAVILY_API_KEY", "").strip(),
        os.environ.get("EXA_API_KEY", "").strip(),
    )

# ======================================================================
#  工具定义
# ======================================================================

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "在互联网上搜索信息。遇到不确定的词汇、文化概念、专有名词时使用。"
            "请用日语或中文关键词搜索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词"
                }
            },
            "required": ["query"],
        },
    }
}

OUTPUT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": (
            "すべての審査が完了したら、この関数を呼び出して修正結果を提出してください。\n"
            "lines配列の各要素は、入力された字幕の同じ位置の行に対応します。\n"
            "修正が不要な行は原文のまま、修正した行は修正後のテキストを入れてください。\n"
            "配列の長さは必ず入力された字幕の行数と一致させてください。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "各行の修正後テキスト。入力と同じ順序・同じ数であること。"
                }
            },
            "required": ["lines"]
        }
    }
}

FUSION_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_fusion",
        "description": (
            "2つの字幕を統合した結果を提出してください。\n"
            "subtitles配列の各要素には、timecode（時間軸）とtext（字幕テキスト）を含めてください。\n"
            "時間軸の形式は「HH:MM:SS,mmm --> HH:MM:SS,mmm」としてください。\n"
            "番号は不要です（自動付与されます）。\n"
            "片方のモデルにしか存在しない発話も必ず含めてください。\n"
            "公式台本とWhisper出力が著しく不一致する場合\n"
            "（別作品の台本・内容の交差なし・音声と台本の完全なミスマッチ等）、\n"
            "subtitlesを空配列にし、errorフィールドに理由を日本語で記入してください。\n"
            "ただし部分的不一致は修正対象であり、error報告の対象外とします。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subtitles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "timecode": {
                                "type": "string",
                                "description": "時間軸（HH:MM:SS,mmm --> HH:MM:SS,mmm）"
                            },
                            "text": {
                                "type": "string",
                                "description": "字幕テキスト"
                            }
                        },
                        "required": ["timecode", "text"]
                    }
                },
                "error": {
                    "type": "string",
                    "description": "公式台本とWhisper出力が著しく不一致する場合の理由。subtitlesが空のときのみ使用。"
                }
            },
            "required": []
        }
    }
}

# ======================================================================
#  联网搜索（Tavily + Exa，V3.5 起替换智谱）
# ======================================================================

def _normalize_query(query):
    return ' '.join(query.strip().lower().split())

def _http_post_json(url, headers, payload, timeout=15):
    """HTTP POST JSON 请求（标准库 urllib，不引入第三方依赖）"""
    import urllib.request
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _tavily_search(query, key, max_results=5):
    """Tavily 搜索（https://docs.tavily.com/）→ [(title, url, snippet), ...]"""
    data = _http_post_json(
        "https://api.tavily.com/search",
        {"Authorization": f"Bearer {key}"},
        {"query": query, "search_depth": "basic", "max_results": max_results},
    )
    return [
        (r.get("title") or "", r.get("url", ""), (r.get("content") or "")[:400])
        for r in data.get("results", [])
        if r.get("url")
    ]

def _exa_search(query, key, max_results=5):
    """Exa 搜索（https://docs.exa.ai/）→ [(title, url, snippet), ...]"""
    data = _http_post_json(
        "https://api.exa.ai/search",
        {"x-api-key": key},
        {"query": query, "numResults": max_results,
         "contents": {"text": {"maxCharacters": 400}}},
    )
    return [
        (r.get("title") or "", r.get("url", ""), (r.get("text") or "")[:400])
        for r in data.get("results", [])
        if r.get("url")
    ]

def web_search(query):
    """联网搜索统一入口（替换原 zhipu_search）。已配置的提供商全部查询：
    Tavily + Exa 结果按 URL 去重合并；仅配置一个时只用该提供商。
    带查询缓存（命中直接返回，不消耗 API 次数）。"""
    global _search_cache, _search_cache_hits, _search_cache_misses, _usage
    normalized = _normalize_query(query)
    if not normalized:
        return "搜索关键词为空"
    cache_key = hashlib.md5(normalized.encode('utf-8')).hexdigest()
    if cache_key in _search_cache:
        _search_cache_hits += 1
        return _search_cache[cache_key] + " [缓存命中]"

    tavily_key, exa_key = get_search_keys()
    if not tavily_key and not exa_key:
        return "搜索不可用：未配置 TAVILY_API_KEY / EXA_API_KEY"

    _search_cache_misses += 1
    entries, errors = [], []

    def _run(name, fn):
        try:
            entries.extend(fn())
            _usage[name + "_calls"] += 1
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"  ⚠ {name} 搜索失败: {e}")

    if tavily_key:
        _run("tavily", lambda: _tavily_search(normalized, tavily_key))
    if exa_key:
        _run("exa", lambda: _exa_search(normalized, exa_key))

    if not entries:
        # Bug #5 修复（沿用）：不缓存错误结果
        return "搜索失败: " + "; ".join(errors)

    # 按 URL 去重（保留先出现的）并合并为文本，限制总长避免撑爆模型上下文
    seen, lines = set(), []
    for title, url, snippet in entries:
        if url in seen:
            continue
        seen.add(url)
        head = f"· {title}（{url}）" if title else f"· {url}"
        lines.append(f"{head}\n  {snippet}" if snippet else head)
    result = "\n".join(lines)
    if len(result) > 3500:
        result = result[:3500] + "…"
    _search_cache[cache_key] = result
    return result

# ======================================================================
#  降级提取函数（从分析报告格式中提取 [N] 行）
# ======================================================================

def try_extract_lines_from_analysis(raw_response, expected_count):
    """当 AI 输出分析报告而非纯文本行时，尝试从中提取 [N] 开头的行。"""
    pattern = re.findall(
        r'^[\[（(](\d+)[\]）)]\s*(.+?)$',
        raw_response, re.MULTILINE
    )
    if not pattern:
        return None
    extracted = {}
    for idx_str, text in pattern:
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        text = re.sub(r'[（(][^)）]*[)）]\s*$', '', text).strip()
        text = re.sub(r'（[^）]*）', '', text).strip()
        text = re.sub(r'^\[\d+\]\s*', '', text).strip()
        if text:
            extracted[idx] = text
    if len(extracted) < expected_count * 0.5:
        return None
    result = []
    for i in range(1, expected_count + 1):
        result.append(extracted.get(i, ""))
    return result

# ======================================================================
#  三层兜底解析
# ======================================================================

def parse_review_result(result, expected_count, original_texts,
                        per_line_retry_fn=None, verbose=True,
                        log_prefix=""):
    if result is None:
        if verbose:
            print(f"{log_prefix}❌ 失败，保留原文")
        return list(original_texts), 0

    parts = result.split("---SPLIT---")
    parts = [p.strip() for p in parts]
    parts = [p for p in parts if p]

    if len(parts) == expected_count:
        changed = sum(1 for o, n in zip(original_texts, parts) if o.strip() != n.strip())
        if verbose:
            print(f"{log_prefix}✅（{changed} 条修改）")
        return parts, 1

    if verbose:
        print(f"{log_prefix}⚠ 件数不一致（{len(parts)}/{expected_count}），降级提取...")
    fallback = try_extract_lines_from_analysis(result, expected_count)
    if fallback:
        result_list = []
        changed = 0
        for orig, fb in zip(original_texts, fallback):
            text = fb.strip() if fb else orig
            result_list.append(text)
            if orig.strip() != text:
                changed += 1
        if verbose:
            print(f"{log_prefix}   ✅ 降级成功（{changed} 条修改）")
        return result_list, 2

    if per_line_retry_fn:
        if verbose:
            print(f"{log_prefix}   ❌ 降级失败，逐条重试...")
        result_list = []
        for i in range(expected_count):
            single = per_line_retry_fn(i)
            if single and single.strip():
                result_list.append(single.strip())
            else:
                result_list.append(original_texts[i])
            time.sleep(0.15)
        if verbose:
            print(f"{log_prefix}   ✅ 逐条重试完成")
        return result_list, 3
    else:
        if verbose:
            print(f"{log_prefix}   ❌ 降级失败，保留原文")
        return list(original_texts), 0

# ======================================================================
#  LLM 统一入口
# ======================================================================

def _default_output_parser(args):
    """OUTPUT_TOOL 默认解析器：提取 lines 并用 ---SPLIT--- 连接"""
    lines = args.get("lines", [])
    return "\n---SPLIT---\n".join(lines) if lines else None

def call_deepseek(client, system_prompt, user_content,
                  retries=3, verbose=True,
                  enable_search=False, max_search_rounds=3,
                  output_tool=None,
                  output_tool_parser=None,
                  stream=False,
                  log_prefix="",
                  show_reasoning=False):
    """
    统一 LLM API 调用入口。

    参数:
        output_tool: 结构化输出工具定义 (OUTPUT_TOOL / CORRECTION_TOOL 等)
        output_tool_parser: 从工具参数中提取结果的函数 (args_dict) -> result
                            返回 None 表示解析失败，回退到纯文本
        stream: 是否使用流式输出
        enable_search: 是否启用联网搜索
    """
    if enable_search and not any(get_search_keys()):
        if verbose:
            print("  ⚠ 未配置 TAVILY_API_KEY / EXA_API_KEY，关闭搜索")
        enable_search = False

    if stream:
        return _call_streaming(
            client, system_prompt, user_content, retries, verbose,
            log_prefix, show_reasoning
        )

    if output_tool and output_tool_parser is None:
        output_tool_parser = _default_output_parser

    return _call_non_streaming(
        client, system_prompt, user_content, retries, verbose,
        output_tool, output_tool_parser,
        enable_search, max_search_rounds,
        log_prefix, show_reasoning
    )

# ======================================================================
#  路径: 纯流式
# ======================================================================

def _call_streaming(client, system_prompt, user_content, retries, verbose,
                    log_prefix="", show_reasoning=False):
    global _usage
    _, _, model, _, max_tokens = get_llm_config()
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.25, max_tokens=max_tokens, stream=True,
                stream_options={"include_usage": True},
                extra_body=_build_extra_body(65536)
            )
            reasoning_parts, content_parts = [], []
            usage_recorded = False
            for chunk in resp:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    r = getattr(delta, 'reasoning_content', None) or ''
                    if r:
                        reasoning_parts.append(r)
                    c = getattr(delta, 'content', None) or ''
                    if c:
                        content_parts.append(c)
                # usage 只在最后一个 chunk 中非 null，用 flag 防止重复计数
                if not usage_recorded and hasattr(chunk, 'usage') and chunk.usage:
                    _add_usage(chunk.usage)
                    usage_recorded = True
            _usage["llm_calls"] += 1
            rt = ''.join(reasoning_parts)
            fc = ''.join(content_parts)
            if verbose:
                if show_reasoning and rt:
                    print(f"{log_prefix}🧠 思考（{len(rt)} 字符）")
                if fc:
                    print(f"{log_prefix}📝 回答（{len(fc)} 字符）")
                elif rt:
                    print(f"{log_prefix}⚠ 回答为空！可能 max_tokens 不够")
            return fc
        except Exception as e:
            print(f"{log_prefix}⚠ API 错误 (第{attempt+1}次): {e}")
            time.sleep(3)
    return None

# ======================================================================
#  路径: 非流式（统一结构化输出 + 搜索 + 自动重试）
# ======================================================================

def _call_non_streaming(client, system_prompt, user_content, retries, verbose,
                        output_tool=None, output_tool_parser=None,
                        enable_search=False, max_search_rounds=3,
                        log_prefix="", show_reasoning=False):
    global _usage
    cache_before_hits = _search_cache_hits
    _, _, model, _, max_tokens = get_llm_config()

    tools = []
    if enable_search:
        tools.append(SEARCH_TOOL)
    if output_tool:
        tools.append(output_tool)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]
    search_count = 0
    tool_call_count = 0

    num_rounds = max_search_rounds + 1 if enable_search else 1

    # Bug #9 修复：显式初始化 msg
    msg = None

    for round_idx in range(num_rounds):
        if verbose and round_idx > 0:
            hits_this_session = _search_cache_hits - cache_before_hits
            print(f"{log_prefix}🔄 第 {round_idx} 轮推理（搜索 {search_count} 次，缓存命中 {hits_this_session} 次）")

        # Bug #3 修复：最后一轮只移除搜索工具，保留 output_tool
        round_tools = None
        if tools:
            if enable_search and round_idx >= max_search_rounds:
                # 最后一轮：只保留 output_tool，移除搜索工具
                round_tools = [output_tool] if output_tool else None
            else:
                round_tools = tools

        resp = None
        for attempt in range(retries):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=round_tools,
                    temperature=0.25, max_tokens=max_tokens, stream=False,
                    extra_body=_build_extra_body(65536)
                )
                _usage["llm_calls"] += 1
                if hasattr(resp, 'usage') and resp.usage:
                    _add_usage(resp.usage)
                break
            except Exception as e:
                print(f"{log_prefix}⚠ API 错误 (第{attempt+1}次): {e}{_api_error_hint(e)}")
                time.sleep(3)

        if resp is None:
            return None

        msg = resp.choices[0].message
        reasoning = getattr(msg, 'reasoning_content', None) or ''
        if verbose and show_reasoning and reasoning:
            print(f"{log_prefix}🧠 思维链 第{round_idx+1}轮（{len(reasoning)} 字符）")

        # ------ 检查输出工具调用 ------
        if msg.tool_calls and output_tool and output_tool_parser:
            for tc in msg.tool_calls:
                if tc.function.name == output_tool["function"]["name"]:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    try:
                        parsed = output_tool_parser(args)
                    except Exception:
                        parsed = None
                    if parsed is not None:
                        if verbose:
                            tool_name = output_tool["function"]["name"]
                            if isinstance(parsed, str):
                                parts_count = len([p for p in parsed.split("---SPLIT---") if p.strip()]) if parsed else 0
                                print(f"{log_prefix}📝 {tool_name} → {parts_count} 条")
                            elif isinstance(parsed, list):
                                if len(parsed) == 0:
                                    print(f"{log_prefix}📝 {tool_name} → 无需修改")
                                else:
                                    print(f"{log_prefix}📝 {tool_name} → {len(parsed)} 条修正")
                            hits_this_session = _search_cache_hits - cache_before_hits
                            if tool_call_count > 0:
                                print(f"{log_prefix}📊 搜索：请求 {tool_call_count} 次，实际 {search_count} 次，缓存命中 {hits_this_session} 次")
                        return parsed

        # ------ 判断是否有搜索调用 ------
        has_search = (
            msg.tool_calls
            and enable_search
            and round_idx < max_search_rounds
            and any(tc.function.name == "web_search" for tc in msg.tool_calls)
        )

        if has_search:
            search_tool_calls = [
                tc for tc in msg.tool_calls
                if tc.function.name == "web_search"
            ]
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in search_tool_calls
                ]
            })
            for tc in msg.tool_calls:
                if tc.function.name == "web_search":
                    tool_call_count += 1
                    try:
                        args = json.loads(tc.function.arguments)
                        query = args.get("query", "")
                    except json.JSONDecodeError:
                        query = tc.function.arguments.strip()
                    if verbose:
                        print(f"{log_prefix}🔍 搜索：\"{query}\"")
                    cache_before = _search_cache_hits
                    result = web_search(query)
                    cache_after = _search_cache_hits
                    if cache_after > cache_before:
                        if verbose:
                            print(f"{log_prefix}   💾 缓存命中")
                    else:
                        search_count += 1
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            content = msg.content or ""

            # Bug #7 修复：空内容重试前先更新 messages
            if not content:
                if verbose:
                    print(f"{log_prefix}⚡ content 为空，自动重试（极简思考）...")

                # 将空响应加入 messages 上下文
                if msg.tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                            for tc in msg.tool_calls
                        ]
                    })
                    # Bug #10 修复：带 tool_calls 的 assistant 消息之后必须跟随
                    # 对应的 tool 响应消息，否则下一次请求 API 返回 400
                    # （insufficient tool messages following tool_calls message）。
                    # 此处用占位响应补齐协议，让模型继续生成。
                    for tc in msg.tool_calls:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "（空）"
                        })
                else:
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or ""
                    })

                try:
                    resp2 = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        tools=round_tools,
                        temperature=0.25, max_tokens=max_tokens, stream=False,
                        extra_body=_build_extra_body(4096)
                    )
                    _usage["llm_calls"] += 1
                    if hasattr(resp2, 'usage') and resp2.usage:
                        _add_usage(resp2.usage)
                    msg2 = resp2.choices[0].message

                    if msg2.tool_calls and output_tool and output_tool_parser:
                        for tc in msg2.tool_calls:
                            if tc.function.name == output_tool["function"]["name"]:
                                try:
                                    args = json.loads(tc.function.arguments)
                                except json.JSONDecodeError:
                                    args = {}
                                try:
                                    parsed = output_tool_parser(args)
                                except Exception:
                                    parsed = None
                                if parsed is not None:
                                    if verbose:
                                        tool_name = output_tool["function"]["name"]
                                        if isinstance(parsed, str):
                                            print(f"{log_prefix}   ✅ 重试成功 → {tool_name}")
                                        elif isinstance(parsed, list):
                                            print(f"{log_prefix}   ✅ 重试成功 → {tool_name} {len(parsed)} 条")
                                    return parsed

                    content2 = msg2.content or ""
                    if content2:
                        if verbose:
                            print(f"{log_prefix}   ✅ 重试成功（{len(content2)} 字符）")
                        return content2
                except Exception as e:
                    if verbose:
                        print(f"{log_prefix}   ❌ 重试失败: {e}{_api_error_hint(e)}")

            if verbose:
                content_len = len(content)
                if output_tool:
                    print(f"{log_prefix}⚠ 未调用 {output_tool['function']['name']}，回退纯文本（{content_len} 字符）")
                else:
                    print(f"{log_prefix}📝 最终回答（{content_len} 字符）")

            hits_this_session = _search_cache_hits - cache_before_hits
            if verbose and tool_call_count > 0:
                print(f"{log_prefix}📊 搜索：请求 {tool_call_count} 次，实际 {search_count} 次，缓存命中 {hits_this_session} 次")

            return content

    print(f"{log_prefix}⚠ 达到最大搜索轮数（{max_search_rounds}），强制输出")
    return msg.content if msg and hasattr(msg, 'content') else ""

# ======================================================================
#  SRT 工具函数
# ======================================================================

def extract_srt_from_response(response_text):
    m = re.search(r'```(?:srt|subtitles)?\s*\n(.*?)\n\s*```', response_text, re.DOTALL)
    if m and '-->' in m.group(1): return m.group(1).strip()
    m = re.search(r'```(?:srt)?\s*\n(.*?)$', response_text, re.DOTALL)
    if m and '-->' in m.group(1): return m.group(1).strip()
    m = re.search(r'\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}', response_text)
    if m:
        start = m.start()
        ls = response_text.rfind('\n', 0, start)
        if ls == -1: ls = 0
        pb = response_text.rfind('\n\n', 0, ls)
        if pb != -1 and pb > ls - 50: ls = pb + 2
        candidate = response_text[ls:].strip()
        if '-->' in candidate: return candidate
    return response_text.strip()

def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def read_text_file(path):
    """无损读取发行商台本，兼容单编码及按行拼接的混合编码文件。"""
    return read_script_text_exact(path)

def parse_srt(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    raw_blocks = re.split(r'\n\n+', content.strip())
    subs = []
    for i, block in enumerate(raw_blocks, start=1):
        lines = block.strip().split('\n')
        if not lines:
            continue
        # 检查第一行是否是序号
        if re.match(r'^\d+$', lines[0]):
            # 标准格式：序号、时间轴、文本
            if len(lines) >= 3:
                ts_match = re.match(
                    r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})',
                    lines[1]
                )
                if ts_match:
                    subs.append({
                        "index": int(lines[0]),
                        "start": ts_match.group(1),
                        "end": ts_match.group(2),
                        "text": '\n'.join(lines[2:]).strip().replace('\n', ' ')
                    })
        else:
            # 无序号：第一行是时间轴
            ts_match = re.match(
                r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})',
                lines[0]
            )
            if ts_match:
                subs.append({
                    "index": i,
                    "start": ts_match.group(1),
                    "end": ts_match.group(2),
                    "text": '\n'.join(lines[1:]).strip().replace('\n', ' ')
                })
    # Bug #1, #2 修复：return 移到循环外，两个分支都能走到
    return subs

def parse_srt_full(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    raw_blocks = re.split(r'\n\n+', content.strip())
    subs = []
    for block in raw_blocks:
        lines = block.strip().split('\n')
        # Bug #8 修复：len(lines) >= 4 已保证 lines[3] 存在，无需再判断
        if len(lines) >= 4:
            # 修复：index 非数字（用户手改字幕/外部工具产物）时跳过该块并警告，
            # 原实现直接 int() 崩溃且不提示是哪个文件哪一行
            try:
                idx = int(lines[0])
            except ValueError:
                print(f"⚠ 跳过格式异常块（首行非序号）：{lines[0][:40]!r} ...")
                continue
            subs.append({
                "index": idx,
                "timecode": lines[1],
                "text_ja": lines[2],
                "text_zh": '\n'.join(lines[3:]).strip(),
            })
    return subs, content
