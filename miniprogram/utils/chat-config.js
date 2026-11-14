// 本地微信登录联调时填入 HTTPS 网关源地址，例如 https://127.0.0.1:8443。
// 读接口联调也需开启服务端 MINIPROGRAM_WECHAT_READ_ENABLED。
// 体验版和正式版必须使用已登记的 HTTPS 域名，并启用读接口鉴权。
module.exports = { wechatChatOrigin: '', wechatReadEnabled: false }
