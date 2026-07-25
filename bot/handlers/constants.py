from __future__ import annotations

CB_NEW_PROXY = "new_proxy"
CB_MY_PROXIES = "my_proxies"
CB_BACK_MENU = "back_menu"
CB_BACK = "back"
CB_USE_ROOT = "use_root"
CB_SPEED_TEST = "speed_test"
CB_DEPLOY_PROXY = "deploy_proxy"
CB_CONTINUE_DEPLOY = "continue_deploy"
CB_MODIFY_PORT = "modify_port"
CB_RETRY_CONNECT = "retry_connect"
CB_NOOP = "noop"
CB_PROXY_BACK = "proxy_back"
CB_MY_PAGE_PREFIX = "my_page:"
CB_PUBLIC_COUNTRIES = "public_countries"
CB_PUBLIC_COUNTRY_PAGE_PREFIX = "public_cp:"
CB_PUBLIC_LOCATION_PREFIX = "public_loc:"
CB_PUBLIC_LOCATION_PAGE_PREFIX = "public_page:"
CB_PROXY_OPEN_PREFIX = "proxy_open:"
CB_PROXY_CHECK_PREFIX = "proxy_check:"
CB_PROXY_TOGGLE_PUBLIC_PREFIX = "toggle_public:"
CB_PROXY_RESET_PREFIX = "proxy_reset:"
CB_PROXY_DELETE_PREFIX = "proxy_delete:"
CB_MODIFY_PORT_DETAIL_PREFIX = "modify_port_detail:"
CB_MODIFY_REMARK_PREFIX = "modify_remark:"
CB_CONFIRM_RESET_PREFIX = "confirm_reset:"
CB_CONFIRM_DELETE_PREFIX = "confirm_delete:"
CB_PROTOCOL_MTPROXY = "protocol:mtproxy"
CB_PROTOCOL_SOCKS5 = "protocol:socks5"
ADMIN_NOTIFY_NEW = "new_proxy"
ADMIN_NOTIFY_OVERWRITE = "overwrite_proxy"
ADMIN_NOTIFY_RESET = "reset_proxy"
ADMIN_NOTIFY_DELETE = "delete_proxy"

PROTOCOL_MTPROXY = "mtproxy"
PROTOCOL_SOCKS5 = "socks5"

STATE_MENU = "menu"
STATE_AWAIT_IP = "await_ip"
STATE_AWAIT_USER = "await_user"
STATE_AWAIT_PASS = "await_pass"
STATE_AWAIT_PROXY_PORT = "await_proxy_port"
STATE_AWAIT_DETAIL_PORT = "await_detail_port"
STATE_AWAIT_REMARK = "await_remark"
STATE_AWAIT_REMARK_INIT = "await_remark_init"
STATE_AWAIT_PROTOCOL = "await_protocol"
STATE_TOOLS = "tools"
STATE_QUICK_CHECK = "quick_check"
STATE_DEPLOYING = "deploying"
STATE_VIEW_MY = "view_my"
STATE_VIEW_PUBLIC = "view_public"

BTN_GREEN = "success"
BTN_RED = "danger"
BTN_BLUE = "primary"

MY_PROXIES_PAGE_SIZE = 6
COUNTRY_PAGE_SIZE = 12
PUBLIC_PROXIES_PAGE_SIZE = 8

# Reply Keyboard 按钮标准文案（与 Telegram 原生 ReplyKeyboard 的 text 一致）。
# 用户发送这些文本等价于点击对应快捷按钮。同义词集合覆盖带 emoji 前缀等旧写法，
# 后续只需修改这三个集合，不用全仓搜索替换。
REPLY_TEXT_NEW_PROXY = "新建代理"
REPLY_TEXT_MY_PROXIES = "我的代理"
REPLY_TEXT_PUBLIC_PROXIES = "公开代理"

REPLY_SYNONYMS_NEW: frozenset[str] = frozenset({REPLY_TEXT_NEW_PROXY, "🚀 新建代理"})
REPLY_SYNONYMS_MY: frozenset[str] = frozenset({REPLY_TEXT_MY_PROXIES, "📁 我的代理"})
REPLY_SYNONYMS_PUBLIC: frozenset[str] = frozenset({REPLY_TEXT_PUBLIC_PROXIES, "🌐 公开代理"})
