from __future__ import annotations

LEGACY_LOCATION_ALIASES: dict[str, str] = {
    "中国香港": "香港",
    "中国澳门": "澳门",
    "中国台湾": "台湾",
}


# ISO 3166-1 alpha-2 → 简体中文。deployer 侦测 IP 归属国后用这张表翻译；
# 公开代理聚合、展示、国旗全部由此表派生，统一维护。
COUNTRY_ZH_MAP: dict[str, str] = {
    "CN": "中国",
    "HK": "香港",
    "MO": "澳门",
    "TW": "台湾",
    "SG": "新加坡",
    "JP": "日本",
    "KR": "韩国",
    "US": "美国",
    "GB": "英国",
    "DE": "德国",
    "FR": "法国",
    "NL": "荷兰",
    "CA": "加拿大",
    "AU": "澳大利亚",
    "RU": "俄罗斯",
    "IN": "印度",
    "TH": "泰国",
    "VN": "越南",
    "MY": "马来西亚",
    "ID": "印度尼西亚",
    "PH": "菲律宾",
    "TR": "土耳其",
    "PL": "波兰",
    "UA": "乌克兰",
    "IE": "爱尔兰",
    "SE": "瑞典",
    "CH": "瑞士",
    "NO": "挪威",
    "FI": "芬兰",
    "DK": "丹麦",
    "ES": "西班牙",
    "IT": "意大利",
    "BE": "比利时",
    "AT": "奥地利",
    "CZ": "捷克",
    "RO": "罗马尼亚",
    "GR": "希腊",
    "HU": "匈牙利",
    "PT": "葡萄牙",
    "BR": "巴西",
    "MX": "墨西哥",
    "AR": "阿根廷",
    "CL": "智利",
    "IL": "以色列",
    "AE": "阿联酋",
    "SA": "沙特阿拉伯",
    "QA": "卡塔尔",
    "KZ": "哈萨克斯坦",
    "PK": "巴基斯坦",
    "BD": "孟加拉国",
    "NZ": "新西兰",
    "ZA": "南非",
    "LU": "卢森堡",
    "BG": "保加利亚",
    "HR": "克罗地亚",
    "RS": "塞尔维亚",
    "SI": "斯洛文尼亚",
    "SK": "斯洛伐克",
    "LT": "立陶宛",
    "LV": "拉脱维亚",
    "EE": "爱沙尼亚",
    "BY": "白俄罗斯",
    "MD": "摩尔多瓦",
    "IS": "冰岛",
    "AL": "阿尔巴尼亚",
    "MK": "北马其顿",
    "GE": "格鲁吉亚",
    "AM": "亚美尼亚",
    "AZ": "阿塞拜疆",
    "EG": "埃及",
    "MA": "摩洛哥",
    "TN": "突尼斯",
    "NG": "尼日利亚",
    "KE": "肯尼亚",
    "ET": "埃塞俄比亚",
    "GH": "加纳",
    "DZ": "阿尔及利亚",
    "IR": "伊朗",
    "IQ": "伊拉克",
    "JO": "约旦",
    "LB": "黎巴嫩",
    "KW": "科威特",
    "OM": "阿曼",
    "BH": "巴林",
    "UY": "乌拉圭",
    "PE": "秘鲁",
    "CO": "哥伦比亚",
    "VE": "委内瑞拉",
    "EC": "厄瓜多尔",
    "PA": "巴拿马",
    "CR": "哥斯达黎加",
    "DO": "多米尼加",
    "GT": "危地马拉",
    "NP": "尼泊尔",
    "LK": "斯里兰卡",
    "MM": "缅甸",
    "KH": "柬埔寨",
    "LA": "老挝",
    "MN": "蒙古",
    "UZ": "乌兹别克斯坦",
    "KG": "吉尔吉斯斯坦",
    "TJ": "塔吉克斯坦",
}

# 反向映射：中文 → ISO code，供展示层从 DB 里的中文 location 推旗帜。
# 若同一中文对应多个 ISO（理论上不会），以先出现者为准。
_ZH_TO_CODE: dict[str, str] = {zh: code for code, zh in COUNTRY_ZH_MAP.items()}

_REGIONAL_INDICATOR_BASE = 0x1F1E6  # 'A' 对应的 regional indicator 起点


def normalize_location(location: str) -> str:
    """把历史里的 '中国香港/澳门/台湾' 等旧写法统一到短名。"""
    name = (location or "").strip()
    if not name:
        return ""
    return LEGACY_LOCATION_ALIASES.get(name, name)


def code_to_flag(code: str) -> str:
    """ISO 3166-1 alpha-2 → 国旗 emoji（由两个 regional indicator 组成）。"""
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return "🌐"
    return "".join(chr(_REGIONAL_INDICATOR_BASE + ord(ch) - ord("A")) for ch in code)


def location_to_code(location: str) -> str | None:
    """中文 location → ISO code，未命中返回 None。"""
    name = normalize_location(location)
    if not name:
        return None
    return _ZH_TO_CODE.get(name)


def country_flag(location: str) -> str:
    code = location_to_code(location)
    if code:
        return code_to_flag(code)
    return "🌐"


def country_short_name(location: str) -> str:
    name = normalize_location(location)
    if not name:
        return "未知"
    return name
