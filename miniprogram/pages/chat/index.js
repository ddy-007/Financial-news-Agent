const { askQuestion, requestErrorMessage } = require('../../utils/api')
const { formatAnswer } = require('../../utils/answer')

const MAX_QUESTION_LENGTH = 1000
const MAX_HISTORY_TURNS = 3
const MAX_HISTORY_LENGTH = 8000
const TRUNCATED_ANSWER = '\n[较长回答已截断]'

function requestHistory(turns) {
  const history = []
  let remaining = MAX_HISTORY_LENGTH
  for (let index = turns.length - 1; index >= 0; index--) {
    const { question, answer } = turns[index]
    if (question.length + answer.length > remaining) {
      if (!history.length) {
        const limit = remaining - question.length - TRUNCATED_ANSWER.length
        const prefix = answer.slice(0, limit).replace(/[\uD800-\uDBFF]$/, '')
        history.unshift([question, prefix + TRUNCATED_ANSWER])
      }
      break
    }
    history.unshift([question, answer])
    remaining -= question.length + answer.length
  }
  return history
}

Page({
  data: {
    query: '', hasQuery: false, question: '', answer: '', answerBlocks: [], sending: false, error: '',
    recentQuestions: [], activeTurn: -1,
    maxQuestionLength: MAX_QUESTION_LENGTH, queryLength: 0, elapsedSeconds: 0
  },

  onInput(event) {
    const query = event.detail.value
    this.setData({ query, queryLength: query.length, hasQuery: Boolean(query.trim()) })
  },

  onUnload() {
    this._unloaded = true
    this.stopWaitTimer()
    this._turns = []
  },

  newConversation() {
    if (this.data.sending || this._unloaded) return
    this._turns = []
    this.setData({
      query: '', queryLength: 0, hasQuery: false, question: '', answer: '', answerBlocks: [],
      recentQuestions: [], activeTurn: -1, error: '', elapsedSeconds: 0
    })
  },

  selectTurn(event) {
    const index = Number(event.currentTarget.dataset.index)
    const turn = this._turns && this._turns[index]
    if (turn && index !== this.data.activeTurn) this.showTurn(turn, index)
  },

  showTurn({ question, answer }, index, update = {}) {
    this.setData({ ...update, question, answer, answerBlocks: formatAnswer(answer), activeTurn: index })
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

    const turns = this._turns || []
    this.setData({ sending: true, error: '', elapsedSeconds: 0 })
    const startedAt = Date.now()
    this._waitTimer = setInterval(() => {
      if (!this._unloaded) {
        const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
        this.setData({ elapsedSeconds })
      }
    }, 1000)
    try {
      const response = await askQuestion(query, requestHistory(turns))
      if (this._unloaded) return
      const answer = response && typeof response.answer === 'string' ? response.answer.trim() : ''
      if (!answer) {
        this.setData({ error: '未收到有效回答，请稍后重试。' })
        return
      }
      const turn = { question: query, answer }
      this._turns = [...turns, turn].slice(-MAX_HISTORY_TURNS)
      this.showTurn(turn, this._turns.length - 1, {
        query: '', queryLength: 0, hasQuery: false,
        recentQuestions: this._turns.map((item, index) => ({ index, question: item.question })).reverse()
      })
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
