"""Claude API 叙事层：把 V1 六维度打分结果（结构化 JSON）翻译成中文判断段落
和可执行建议。LLM 在这里只做文字转述，不做任何数字计算（guidebook 0 总原则）。

所有 Claude API 调用集中在本文件，便于控制成本和更换模型（guidebook 6.2）。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

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

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set; skipping narrative generation")
        return {
            "narrative": "叙事生成不可用：未配置 ANTHROPIC_API_KEY。以下为结构化打分结果，"
                         "请人工参考各维度数据自行判断。",
            "suggestions": [],
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
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
