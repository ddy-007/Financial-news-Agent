const { askQuestion } = require('../../utils/api')

Page({
  data: { query: '', hasQuery: false, question: '', answer: '', sending: false, error: '' },

  onInput(event) {
    const query = event.detail.value
    this.setData({ query, hasQuery: Boolean(query.trim()) })
  },

  async sendQuestion() {
    const query = this.data.query.trim()
    if (!query || this.data.sending) return

    this.setData({ sending: true, question: query, answer: '', error: '' })
    try {
      const response = await askQuestion(query)
      const answer = response && typeof response.answer === 'string' ? response.answer.trim() : ''
      if (!answer) throw new Error('未收到回答，请稍后重试。')
      this.setData({ answer })
    } catch (error) {
      this.setData({ error: error.message || '问答请求失败。' })
    } finally {
      this.setData({ sending: false })
    }
  }
})
