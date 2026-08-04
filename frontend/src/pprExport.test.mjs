import test from 'node:test'
import assert from 'node:assert/strict'

import { api, filenameFromContentDisposition } from './api.ts'
import { canShowPprExport, runPprExportDownload, savePprExportFile } from './pprExport.ts'


test('export control is visible only to an active admin', () => {
  assert.equal(canShowPprExport({ role: 'admin', is_active: true }), true)
  assert.equal(canShowPprExport({ role: 'checker', is_active: true }), false)
  assert.equal(canShowPprExport({ role: 'admin', is_active: false }), false)
  assert.equal(canShowPprExport(null), false)
})

test('export request uses relative URL, Telegram auth header, Blob, and response filename', async () => {
  const previousWindow = globalThis.window
  const previousFetch = globalThis.fetch
  const sourceBlob = new Blob(['xlsx-content'], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
  let request
  globalThis.window = {
    Telegram: { WebApp: { initData: 'signed-telegram-init-data' } }
  }
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(sourceBlob, {
      status: 200,
      headers: {
        'Content-Disposition': 'attachment; filename="PPR_export_2026-08-04_12-30.xlsx"'
      }
    })
  }

  try {
    const result = await api.exportPpr()
    assert.equal(request.url, '/api/export/ppr.xlsx')
    assert.equal(request.options.method, 'GET')
    assert.equal(request.options.headers['X-Telegram-Init-Data'], 'signed-telegram-init-data')
    assert.ok(result.blob instanceof Blob)
    assert.equal(await result.blob.text(), 'xlsx-content')
    assert.equal(result.filename, 'PPR_export_2026-08-04_12-30.xlsx')
  } finally {
    globalThis.window = previousWindow
    globalThis.fetch = previousFetch
  }
})

test('download helper clicks a hidden anchor and uses the server filename', () => {
  const blob = new Blob(['xlsx'])
  const calls = []
  const anchor = {
    href: '',
    download: '',
    style: { display: '' },
    click: () => calls.push('click'),
    remove: () => calls.push('remove')
  }
  savePprExportFile(
    { blob, filename: 'PPR_export_2026-08-04_12-30.xlsx' },
    {
      createObjectURL: value => {
        assert.equal(value, blob)
        return 'blob:test-url'
      },
      revokeObjectURL: url => calls.push(`revoke:${url}`),
      createAnchor: () => anchor,
      appendAnchor: value => {
        assert.equal(value, anchor)
        calls.push('append')
      },
      defer: callback => callback()
    }
  )

  assert.equal(anchor.href, 'blob:test-url')
  assert.equal(anchor.download, 'PPR_export_2026-08-04_12-30.xlsx')
  assert.equal(anchor.style.display, 'none')
  assert.deepEqual(calls, ['append', 'click', 'remove', 'revoke:blob:test-url'])
})

test('download error is shown and loading is always reset', async () => {
  const loading = []
  const errors = []
  const saved = []
  const succeeded = await runPprExportDownload(
    async () => { throw new Error('Экспорт временно недоступен') },
    file => saved.push(file),
    value => loading.push(value),
    value => errors.push(value)
  )

  assert.equal(succeeded, false)
  assert.deepEqual(loading, [true, false])
  assert.deepEqual(errors, ['', 'Экспорт временно недоступен'])
  assert.deepEqual(saved, [])
})

test('Content-Disposition parser supports UTF-8 names and rejects unsafe paths', () => {
  assert.equal(
    filenameFromContentDisposition("attachment; filename*=UTF-8''PPR_export_2026-08-04_12-30.xlsx"),
    'PPR_export_2026-08-04_12-30.xlsx'
  )
  assert.equal(
    filenameFromContentDisposition('attachment; filename="../not-an-excel.txt"'),
    'PPR_export.xlsx'
  )
})
