// 模拟器使用回环地址。真机联调时改为电脑的局域网地址。
const BASE_URL = 'http://127.0.0.1:8000'

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
        const detail = body && typeof body.detail === 'string' ? body.detail : ''
        reject(new Error(detail || `请求失败（HTTP ${statusCode}）。`))
      },
      fail({ errMsg = '' } = {}) {
        const message = errMsg.includes('timeout')
          ? '请求超时，请稍后重试。'
          : '无法连接后端，请检查服务地址和网络。'
        reject(new Error(message))
      }
    })
  })
}

function getLatestReport() {
  return request('/api/v1/reports/today', { timeout: 12000 })
}

function askQuestion(query) {
  return request('/api/v1/agent/chat', {
    method: 'POST',
    data: { query, chat_history: [] },
    timeout: 180000
  })
}

module.exports = { getLatestReport, askQuestion }
