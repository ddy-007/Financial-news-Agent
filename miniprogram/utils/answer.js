// Render the Markdown used in answers as native text, never as model-provided HTML.
function inline(text) {
  const spans = []
  const tokens = /\\([\\`*_|])|`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__/g
  let offset = 0
  for (let match; (match = tokens.exec(text));) {
    if (match.index > offset) spans.push({ id: offset, text: text.slice(offset, match.index), style: 'plain' })
    spans.push({
      id: match.index,
      text: match[1] || match[2] || match[3] || match[4],
      style: match[1] ? 'plain' : match[2] ? 'code' : 'strong'
    })
    offset = match.index + match[0].length
  }
  if (offset < text.length) spans.push({ id: offset, text: text.slice(offset), style: 'plain' })
  return spans
}

function tableCells(line) {
  const cells = []
  let start = 0
  // Escaped pipes and pipes in single-backtick code spans belong to the cell.
  const separators = /\\.|`[^`\n]+`|(\|)/g
  for (let match; (match = separators.exec(line));) {
    if (!match[1]) continue
    cells.push(line.slice(start, match.index).trim())
    start = match.index + 1
  }
  if (!cells.length) return []
  cells.push(line.slice(start).trim())
  if (line.trimStart().startsWith('|')) cells.shift()
  if (start === line.trimEnd().length) cells.pop()
  return cells
}

function readTable(lines, start) {
  const headers = tableCells(lines[start])
  const separator = tableCells(lines[start + 1] || '')
  if (headers.length < 2 || headers.length !== separator.length ||
      !separator.every(cell => /^:?-{3,}:?$/.test(cell))) return null

  const rows = []
  const hasOuterPipes = /^\s*\|.*\|\s*$/.test(lines[start])
  let next = start + 2
  for (; next < lines.length; next++) {
    if (hasOuterPipes && !/^\s*\|.*\|\s*$/.test(lines[next])) break
    const cells = tableCells(lines[next])
    if (cells.length !== headers.length) break
    rows.push({
      id: next,
      cells: cells.map((cell, index) => ({
        id: index,
        label: inline(headers[index] || `第 ${index + 1} 列`),
        spans: inline(cell)
      }))
    })
  }
  return rows.length ? { rows, next } : null
}

function formatAnswer(answer) {
  const blocks = []
  const lines = answer.split(/\r\n?|\n/)
  let paragraph = []
  const flush = () => {
    if (!paragraph.length) return
    const text = paragraph.join('\n')
    blocks.push({ kind: /^⚠/.test(text.trimStart()) ? 'notice' : 'paragraph', spans: inline(text) })
    paragraph = []
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (!line.trim()) {
      flush()
      continue
    }

    const fence = /^ {0,3}(`{3,}|~{3,})/.exec(line)
    if (fence) {
      flush()
      const end = new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}\\s*$`)
      const code = []
      while (i + 1 < lines.length && !end.test(lines[i + 1])) code.push(lines[++i])
      if (i + 1 < lines.length) i++
      blocks.push({ kind: 'code', text: code.join('\n') })
      continue
    }

    const heading = /^ {0,3}(#{1,6})\s+(.+)$/.exec(line)
    if (heading) {
      flush()
      blocks.push({ kind: 'heading', level: heading[1].length, spans: inline(heading[2]) })
      continue
    }

    const table = readTable(lines, i)
    if (table) {
      flush()
      blocks.push({ kind: 'table', rows: table.rows })
      i = table.next - 1
      continue
    }

    const item = /^\s*(?:(\d{1,9})[.)]|[-+*])\s+(.+)$/.exec(line)
    if (item) {
      flush()
      blocks.push({ kind: 'item', marker: item[1] ? `${item[1]}.` : '•', spans: inline(item[2]) })
      continue
    }

    paragraph.push(line)
  }
  flush()
  return blocks.map((block, id) => ({ ...block, id }))
}

module.exports = { formatAnswer }
