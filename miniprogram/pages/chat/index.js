const { askQuestion, requestErrorMessage } = require('../../utils/api')

const MAX_QUESTION_LENGTH = 1000

Page({
  data: {
    query: '', hasQuery: false, question: '', answer: '', sending: false, error: '',
    maxQuestionLength: MAX_QUESTION_LENGTH, queryLength: 0, elapsedSeconds: 0
  },

  onInput(event) {
    const query = event.detail.value
    this.setData({ query, queryLength: query.length, hasQuery: Boolean(query.trim()) })
  },

  onUnload() {
    this._unloaded = true
    this.stopWaitTimer()
  },

  stopWaitTimer() {
    if (this._waitTimer != null) {
      clearInterval(this._waitTimer)
      this._waitTimer = null
    }
  },

  async sendQuestion() {
    const query = this.data.query.trim()
    if (!query || this.data.sending || this._unloaded) return
    if (this.data.query.length > MAX_QUESTION_LENGTH) {
      this.setData({ error: `问题最多可输入 ${MAX_QUESTION_LENGTH} 字，请缩短后再发送。` })
      return
    }

    this.setData({ sending: true, question: query, answer: '', error: '', elapsedSeconds: 0 })
    const startedAt = Date.now()
    this._waitTimer = setInterval(() => {
      if (!this._unloaded) {
        const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
        this.setData({ elapsedSeconds })
      }
    }, 1000)
    try {
      const response = await askQuestion(query)
      if (this._unloaded) return
      const answer = response && typeof response.answer === 'string' ? response.answer.trim() : ''
      this.setData(answer ? { answer } : { error: '未收到有效回答，请稍后重试。' })
    } catch (error) {
      if (!this._unloaded) this.setData({ error: requestErrorMessage(error, 'chat') })
    } finally {
      this.stopWaitTimer()
      if (!this._unloaded) this.setData({ sending: false })
    }
  },

  copyAnswer() {
    if (!this.data.answer) return
    wx.setClipboardData({
      data: this.data.answer,
      fail: () => {
        if (!this._unloaded) wx.showToast({ title: '复制失败，请长按答案复制', icon: 'none' })
      }
    })
  }
})
