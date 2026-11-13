// 模拟器使用回环地址。真机联调时改为电脑的局域网地址。
const BASE_URL = 'http://127.0.0.1:8000'

function requestError(kind, statusCode) {
  const error = new Error(kind)
  error.kind = kind
  error.statusCode = statusCode
  return error
}

function requestErrorMessage(error, page) {
  if (error.kind === 'timeout') {
    return page === 'chat'
      ? '等待超时，服务端可能仍在处理，请勿连续发送。'
      : '读取日报超时，请稍后刷新。'
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
  return page === 'chat' ? '问答请求失败，请稍后重试。' : '加载日报失败，请稍后刷新。'
}

function request(path, { method = 'GET', data, timeout }) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${BASE_URL}${path}`,
      method,
      data,
      timeout,
      header: { 'content-type': 'application/json' },
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

function getLatestReport() {
  return request('/api/v1/reports/today', { timeout: 12000 })
}

function getDailyReports() {
  return request('/api/v1/reports?report_type=daily&limit=10', { timeout: 12000 })
}

function askQuestion(query, history = []) {
  return request('/api/v1/agent/chat', {
    method: 'POST',
    data: { query, chat_history: history },
    timeout: 180000
  })
}

module.exports = { getLatestReport, getDailyReports, askQuestion, requestErrorMessage }
