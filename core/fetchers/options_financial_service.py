"""financial-service 期权数据源（首选实现，guidebook 5.6）。

【接入状态：待补全】本文件目前是结构完整的骨架：接口签名、配置读取、错误处理模式
都已按 OptionsFetcher 契约就位，但 financial-service 的 API 文档（base_url、认证方式、
期权链端点、字段名）尚未提供，无法凭空编造请求格式——那会产出看起来能跑、
实际字段全错的代码。

接入时需要补全的三处（搜索 TODO(financial-service)）：
1. _request(): 实际的 HTTP 调用（端点路径、认证 header/参数、分页方式）
2. get_chain(): 服务返回的 JSON -> OptionQuote 的字段映射
   （合约代码/到期日/行权价/买卖价/成交量/OI/IV 分别叫什么、IV 是小数还是百分比）
3. get_risk_free_rate(): 若服务提供利率曲线端点则用之，否则删掉此覆写、
   沿用 yfinance ^IRX 的实现

补全后把 config.yaml 里 v5.financial_service.base_url 填上即可生效，
上层代码零改动（工厂函数在 options_base.get_options_fetcher）。
"""
import os

from core.fetchers.base import FetchResult
from core.fetchers.options_base import OptionsFetcher


class FinancialServiceOptionsFetcher(OptionsFetcher):
    def __init__(self, fs_cfg: dict, fetch_cfg: dict):
        self.base_url = (fs_cfg.get("base_url") or "").strip()
        self.api_key = os.environ.get(fs_cfg.get("api_key_env", "FINANCIAL_SERVICE_API_KEY"), "")
        self.fetch_cfg = fetch_cfg
        if not self.base_url:
            raise NotImplementedError(
                "financial-service 尚未接入：config v5.financial_service.base_url 为空，"
                "且客户端的请求/字段映射待 API 文档提供后补全（见本文件头部说明）"
            )
        if not self.api_key:
            raise NotImplementedError(
                f"financial-service 未配置认证：环境变量 {fs_cfg.get('api_key_env')} 为空"
            )

    def get_chain(self, symbol: str, max_expiries: int = 8) -> FetchResult:
        # TODO(financial-service): 实现期权链拉取与字段映射 -> OptionChainData
        raise NotImplementedError("financial-service get_chain 待 API 文档提供后实现")

    def get_risk_free_rate(self) -> FetchResult:
        # TODO(financial-service): 若服务提供利率曲线则在此实现
        raise NotImplementedError("financial-service get_risk_free_rate 待 API 文档提供后实现")
