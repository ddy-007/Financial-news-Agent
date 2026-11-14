// 开发版可以使用本机 FastAPI。体验版和正式版统一通过鉴权网关。
const BASE_URL = 'http://127.0.0.1:8000'
const { wechatChatOrigin, wechatReadEnabled } = require('./chat-config')
let chatSession
let loginInFlight

function requestError(kind, statusCode) {
  const error = new Error(kind)
  error.kind = kind
  error.statusCode = statusCode
  return error
}

function requestErrorMessage(error, page) {
  if (error.kind === 'config') return '服务地址配置不完整，请联系小程序管理员。'
  if (error.kind === 'login' && error.statusCode === 429) return '微信登录请求过于频繁，请稍后重试。'
  if (error.kind === 'login' && error.statusCode === 403) return '微信暂未允许本次登录。'
  if (error.kind === 'login') return page === 'chat' ? '微信登录失败，请稍后重新发送。' : '微信登录失败，请稍后重试。'
  if (error.kind === 'session') return page === 'chat' ? '微信登录已失效，请重新发送。' : '微信登录已失效，请重新加载。'
  if (error.kind === 'timeout') {
    return page === 'chat'
      ? '等待超时，服务端可能仍在处理，请勿连续发送。'
      : page === 'news' ? '读取新闻超时，请稍后刷新。' : '读取日报超时，请稍后刷新。'
  }
  if (error.kind === 'network') return '无法连接后端，请检查服务地址和网络。'
  if (error.kind === 'http') {
    const status = error.statusCode
    if (status === 404 && page === 'report') return '暂无日报。'
    if (status === 400 || status === 422) return '请求内容或格式有误，请检查后重试。'
    if (status === 401 || status === 403) return '服务访问受限，请检查访问权限。'
    if (status === 429) return '请求过于频繁，请稍后重试。'
    if (status >= 500) return '服务暂时未完成请求，请稍后重试。'
    return `请求失败（HTTP ${status}）。`
  }
  if (page === 'chat') return '问答请求失败，请稍后重试。'
  return page === 'news' ? '加载新闻失败，请稍后刷新。' : '加载日报失败，请稍后刷新。'
}

function request(path, { method = 'GET', data, timeout, origin = BASE_URL, token }) {
  return new Promise((resolve, reject) => {
    const header = { 'content-type': 'application/json' }
    if (token) header.Authorization = `Bearer ${token}`
    wx.request({
      url: `${origin}${path}`,
      method,
      data,
      timeout,
      header,
      success({ statusCode, data: body }) {
        if (statusCode >= 200 && statusCode < 300) {
          resolve(body)
          return
        }
        reject(requestError('http', statusCode))
      },
      fail({ errMsg = '' } = {}) {
        reject(requestError(/timeout|timed out/i.test(errMsg) ? 'timeout' : 'network'))
      }
    })
  })
}

async function loginForChat() {
  if (chatSession && chatSession.expiresAt > Date.now()) return chatSession.token
  if (!loginInFlight) {
    loginInFlight = (async () => {
      const code = await new Promise((resolve, reject) => {
        wx.login({ timeout: 8000, success: ({ code }) => code ? resolve(code) : reject(), fail: reject })
      })
      const startedAt = Date.now()
      const body = await request('/api/v1/miniprogram/login', {
        method: 'POST', data: { code }, timeout: 12000, origin: wechatChatOrigin
      })
      if (!body || typeof body.access_token !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(body.access_token) ||
          !Number.isInteger(body.expires_in) || body.expires_in <= 30 || body.expires_in > 3600) throw requestError('login')
      chatSession = { token: body.access_token, expiresAt: startedAt + (body.expires_in - 30) * 1000 }
      return chatSession.token
    })().catch((error) => { throw requestError('login', error && error.statusCode) })
      .finally(() => { loginInFlight = null })
  }
  return loginInFlight
}

function getLatestReport() {
  return read('/api/v1/reports/today', { timeout: 12000 })
}

function getDailyReports() {
  return read('/api/v1/reports?report_type=daily&limit=10', { timeout: 12000 })
}

function getWatchedNews(keyword) {
  return read('/api/v1/news', { data: { keyword, limit: 20 }, timeout: 12000 })
}

function getEventTimeline(newsId, asOf) {
  return read(`/api/v1/news/${encodeURIComponent(newsId)}/timeline`, {
    data: asOf ? { as_of: asOf } : undefined, timeout: 12000
  })
}

function read(path, options) {
  return configuredRequest(path, options, wechatReadEnabled === true)
}

function versionEnvironment() {
  try {
    return wx.getAccountInfoSync().miniProgram.envVersion
  } catch (_) {
    return ''
  }
}

async function configuredRequest(path, options, useGateway) {
  const environment = versionEnvironment()
  const gateway = typeof wechatChatOrigin === 'string' &&
    /^https:\/\/([a-z0-9.-]+)(?::([0-9]{1,5}))?$/i.exec(wechatChatOrigin)
  const validGateway = gateway && (!gateway[2] || (Number(gateway[2]) > 0 && Number(gateway[2]) <= 65535))
  if (environment === 'trial' || environment === 'release') {
    const host = validGateway && gateway[1]
    if (wechatReadEnabled !== true || !host || /(?:^|\.)localhost$/i.test(host) ||
        !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9]$/i.test(host)) {
      throw requestError('config')
    }
  } else if (environment !== 'develop' || (useGateway && !validGateway)) {
    throw requestError('config')
  }
  if (!useGateway) return request(path, options)

  const token = await loginForChat()
  try {
    return await request(path, { ...options, origin: wechatChatOrigin, token })
  } catch (error) {
    if (error.statusCode === 401) {
      if (chatSession && chatSession.token === token) chatSession = null
      throw requestError('session')
    }
    throw error
  }
}

function askQuestion(query, history = []) {
  return configuredRequest('/api/v1/agent/chat', {
    method: 'POST', data: { query, chat_history: history }, timeout: 180000
  }, Boolean(wechatChatOrigin))
}

module.exports = { getLatestReport, getDailyReports, getWatchedNews, getEventTimeline, askQuestion, requestErrorMessage }
