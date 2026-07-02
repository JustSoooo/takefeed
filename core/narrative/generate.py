"""Claude API 叙事层：V1 把结构化打分翻译成中文判断段落（LLM 只做文字转述，
不做任何数字计算），V3 把抓到的新闻标题分类为利多/利空/中性（LLM 唯一被允许
做的"判断"，且原始标题永远由代码本地拼回、不依赖模型复述，见 guidebook 0 / 4.2）。

所有 Claude API 调用集中在本文件，便于控制成本和更换模型（guidebook 6.2）。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def _get_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=api_key)

_SYSTEM_PROMPT = """你是一名量化研究助理，为一名有多年实盘经验的个人投资者撰写每日市场风险偏好简报。
你收到的六维度打分是已经计算好的确定性结果，你的任务只是把这些结构化数字转译成中文叙述，
绝不允许自己重新判断或编造未包含在输入数据里的数字。

硬性要求：
1. 判断段落 150-250 字，必须至少引用三个具体维度的数字或状态（如"VIX 处于近一年 78 百分位"），
   禁止输出"市场情绪偏乐观"这类没有数据支撑的模糊套话。
2. 给出 2-3 条建议，每条必须是"触发条件式"的预案（例如"若 XX 跌破 YY，则关注 ZZ"），
   不是笼统的"建议谨慎"。
3. 如果某个维度状态是 missing，明确提示该维度数据缺失，不要假装它存在。
4. 只输出建议基调，不输出具体买卖点位和仓位百分比（这是决策支持工具，不是交易信号）。
5. 严格按照给定的 JSON schema 输出，不要输出 schema 之外的任何文字。
"""

_OUTPUT_SCHEMA_HINT = """请仅输出如下 JSON（不要加 markdown 代码块围栏，不要加任何解释文字）：
{"narrative": "150-250字中文判断段落", "suggestions": ["建议1", "建议2", "建议3"]}
"""


def _build_user_prompt(composite_score, state, dim_payload, weights) -> str:
    payload = {
        "composite_score": composite_score,
        "state": state,
        "dimensions": dim_payload,
        "weights_used_today": weights,
    }
    return (
        "以下是今日 V1 六维度打分的结构化结果，请据此生成简报：\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n" + _OUTPUT_SCHEMA_HINT
    )


def generate_v1_narrative(composite_score, state, dim_results: dict, weights: dict, cfg: dict) -> dict:
    """dim_results: dict[str, DimensionResult]-like objects (dataclass or dict) with
    .dimension/.status/.raw/.percentile/.score attributes."""
    dim_payload = {}
    for name, d in dim_results.items():
        status = d.status if hasattr(d, "status") else d["status"]
        dim_payload[name] = {
            "status": status,
            "raw": (d.raw if hasattr(d, "raw") else d.get("raw")) if status == "ok" else None,
            "percentile": (d.percentile if hasattr(d, "percentile") else d.get("percentile")) if status == "ok" else None,
            "score": (d.score if hasattr(d, "score") else d.get("score")) if status == "ok" else None,
            "missing_reason": (d.note if hasattr(d, "note") else d.get("note")) if status == "missing" else None,
        }

    client = _get_client()
    if client is None:
        logger.warning("ANTHROPIC_API_KEY not set; skipping narrative generation")
        return {
            "narrative": "叙事生成不可用：未配置 ANTHROPIC_API_KEY。以下为结构化打分结果，"
                         "请人工参考各维度数据自行判断。",
            "suggestions": [],
        }

    try:
        resp = client.messages.create(
            model=cfg.get("model", "claude-sonnet-5"),
            max_tokens=cfg.get("max_tokens", 1024),
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(composite_score, state, dim_payload, weights)}],
        )
        text = resp.content[0].text.strip()
        parsed = json.loads(text)
        if "narrative" not in parsed or "suggestions" not in parsed:
            raise ValueError("Claude response missing required keys")
        return parsed
    except Exception as exc:
        logger.error("narrative generation failed: %s", exc)
        return {
            "narrative": f"叙事生成失败（{exc}）。以下为结构化打分结果，请人工参考各维度数据自行判断。",
            "suggestions": [],
        }


_NEWS_SYSTEM_PROMPT = """你是一名新闻归纳助理。你会收到一只股票近期的新闻标题列表（已编号）。

硬性要求：
1. 对每一条编号，只判断这条标题本身透露的信息是利多、利空还是中性，不允许预测股价涨跌，
   不允许输出"市场传闻"或标题里没有的信息。
2. 每条给一句话理由（20字以内），理由必须直接引用标题里出现的事实，不要泛泛而谈。
3. 不输出情绪分数，只输出三档标签：利多 / 利空 / 中性。
4. 严格按 schema 输出，且必须覆盖所有编号，不能遗漏或新增编号。
"""

_NEWS_OUTPUT_SCHEMA_HINT = """请仅输出如下 JSON 数组（不要加 markdown 代码块围栏，不要加任何解释文字）：
[{"index": 1, "label": "利多|利空|中性", "reason": "一句话理由"}, ...]
"""


def classify_news_sentiment(symbol: str, headlines: list[dict], cfg: dict) -> dict:
    """headlines: [{"title", "publisher", "link", "published_at"}, ...] already fetched
    deterministically (core/fetchers/us_stock.py). The LLM only assigns a label + reason
    per index; the original headline fields are always spliced back in locally afterward,
    so a displayed item can never show a title the model invented (guidebook 4.2)."""
    if not headlines:
        return {"items": [], "note": "近期无相关新闻"}

    client = _get_client()
    if client is None:
        return {
            "items": [{**h, "label": "未分类", "reason": "未配置 ANTHROPIC_API_KEY"} for h in headlines],
            "note": "舆情分类不可用：未配置 ANTHROPIC_API_KEY",
        }

    numbered = "\n".join(f"{i + 1}. {h['title']}（来源: {h.get('publisher') or '未知'}）" for i, h in enumerate(headlines))
    prompt = f"股票代码: {symbol}\n新闻标题列表:\n{numbered}\n\n{_NEWS_OUTPUT_SCHEMA_HINT}"

    try:
        resp = client.messages.create(
            model=cfg.get("model", "claude-sonnet-5"),
            max_tokens=cfg.get("max_tokens", 1024),
            system=_NEWS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        labels = json.loads(text)
        by_index = {item["index"]: item for item in labels if "index" in item}

        items = []
        for i, h in enumerate(headlines, start=1):
            label_info = by_index.get(i)
            items.append({
                **h,
                "label": label_info["label"] if label_info else "中性",
                "reason": label_info["reason"] if label_info else "模型未返回该条分类，默认中性",
            })
        return {"items": items, "note": None}
    except Exception as exc:
        logger.error("news classification failed for %s: %s", symbol, exc)
        return {
            "items": [{**h, "label": "未分类", "reason": f"分类失败：{exc}"} for h in headlines],
            "note": f"舆情分类失败：{exc}",
        }
