import { useEffect, useMemo, useState } from 'react'
import { createModelConfig, createModelProvider, deleteModelConfig, deleteModelProvider, fetchModelConfigs, fetchModelProviders, testModelConfig, updateModelConfig, updateModelProvider } from '../lib/api'
import { toast } from '../lib/toast'
import type { ModelCapability, ModelConfig, ModelProvider, ModelTestResult } from '../types'

const CAPABILITIES: { value: ModelCapability; label: string; hint: string }[] = [
  { value: 'chat', label: '推理', hint: '讲解、解题、总结、纠错' },
  { value: 'vision', label: '识图', hint: '扫描页、图表、公式识别' },
  { value: 'image_gen', label: '生图', hint: '画图题、示意图、几何图' },
]

const emptyProvider = {
  name: '',
  type: 'openai_compatible',
  base_url: '',
  api_key: '',
  auth_type: 'bearer',
  extra_headers_text: '',
  enabled: true,
  note: '',
}

const emptyModel = {
  provider_id: 0,
  model_name: '',
  capability: 'chat' as ModelCapability,
  enabled: true,
  priority: 100,
  params_text: '',
  note: '',
}

function parseJsonObject(text: string, field: string): Record<string, unknown> {
  const raw = text.trim()
  if (!raw) return {}
  const parsed = JSON.parse(raw)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(`${field} 必须是 JSON 对象`)
  return parsed as Record<string, unknown>
}

export default function ModelSettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [providers, setProviders] = useState<ModelProvider[]>([])
  const [configs, setConfigs] = useState<ModelConfig[]>([])
  const [providerForm, setProviderForm] = useState(emptyProvider)
  const [modelForm, setModelForm] = useState(emptyModel)
  const [editingProviderId, setEditingProviderId] = useState<number | null>(null)
  const [editingModelId, setEditingModelId] = useState<number | null>(null)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [testResults, setTestResults] = useState<Record<number, ModelTestResult>>({})
  const [busy, setBusy] = useState(false)
  const [guideOpen, setGuideOpen] = useState(false)

  const load = async () => {
    const [ps, cs] = await Promise.all([fetchModelProviders(), fetchModelConfigs()])
    setProviders(ps)
    setConfigs(cs)
    setModelForm((prev) => ({ ...prev, provider_id: prev.provider_id || ps[0]?.id || 0 }))
  }

  useEffect(() => {
    if (!open) return
    void load().catch((e) => toast(`加载模型设置失败：${e instanceof Error ? e.message : e}`))
  }, [open])

  useEffect(() => {
    if (!open) setGuideOpen(false)
  }, [open])

  useEffect(() => {
    if (!guideOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setGuideOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [guideOpen])

  const grouped = useMemo(() => CAPABILITIES.map((cap) => {
    const models = configs.filter((m) => m.capability === cap.value)
    const enabled = models.filter((m) => m.enabled && providers.find((p) => p.id === m.provider_id)?.enabled)
    return { ...cap, models, enabled }
  }), [configs, providers])

  if (!open) return null

  const resetProvider = () => {
    setEditingProviderId(null)
    setProviderForm(emptyProvider)
  }
  const resetModel = () => {
    setEditingModelId(null)
    setModelForm({ ...emptyModel, provider_id: providers[0]?.id || 0 })
  }

  const saveProvider = async () => {
    try {
      setBusy(true)
      const extra_headers = parseJsonObject(providerForm.extra_headers_text, '额外请求头') as Record<string, string>
      const payload = {
        name: providerForm.name,
        type: providerForm.type,
        base_url: providerForm.base_url,
        api_key: providerForm.api_key,
        auth_type: providerForm.auth_type,
        extra_headers,
        enabled: providerForm.enabled,
        note: providerForm.note,
      }
      if (editingProviderId) await updateModelProvider(editingProviderId, payload)
      else await createModelProvider(payload)
      resetProvider()
      await load()
      toast('Provider 已保存')
    } catch (e) {
      toast(`保存失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  const saveModel = async () => {
    try {
      setBusy(true)
      const params = parseJsonObject(modelForm.params_text, '参数')
      const payload = {
        provider_id: Number(modelForm.provider_id),
        model_name: modelForm.model_name,
        capability: modelForm.capability,
        enabled: modelForm.enabled,
        priority: Number(modelForm.priority),
        params,
        note: modelForm.note,
      }
      if (editingModelId) await updateModelConfig(editingModelId, payload)
      else await createModelConfig(payload)
      resetModel()
      await load()
      toast('模型配置已保存')
    } catch (e) {
      toast(`保存失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  const runTest = async (m: ModelConfig) => {
    setTestingId(m.id)
    try {
      const res = await testModelConfig({ capability: m.capability, provider_id: m.provider_id, model_name: m.model_name })
      setTestResults((prev) => ({ ...prev, [m.id]: res }))
      toast(res.ok ? `连接成功：${res.latency_ms ?? 0}ms` : `连接失败：${res.error_type ?? 'error'}`)
    } catch (e) {
      toast(`测试失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setTestingId(null)
    }
  }

  return (
    <div className="model-modal-mask" onClick={onClose}>
      <div className="model-modal" onClick={(e) => e.stopPropagation()}>
        <div className="model-head">
          <div>
            <div className="model-title-row">
              <div className="model-title">模型与 API</div>
              <button
                className="model-help-trigger"
                type="button"
                aria-label="打开模型与 API 配置教程"
                title="配置教程"
                onClick={() => setGuideOpen(true)}
              >?</button>
            </div>
            <div className="model-sub">页面配置是唯一运行时配置源 · Key 与敏感请求头仅显示掩码</div>
          </div>
          <button className="model-close" aria-label="关闭模型设置" onClick={onClose}>×</button>
        </div>

        <div className="model-body">
          <section className="model-panel">
            <div className="model-panel-title">Provider</div>
            <div className="provider-list">
              {providers.map((p) => (
                <div key={p.id} className="provider-card">
                  <div className="provider-main">
                    <b>{p.name}</b>
                    <span className={p.enabled ? 'state on' : 'state'}>{p.enabled ? '启用' : '禁用'}</span>
                    <code>{p.masked_key ?? '未填 Key'}</code>
                  </div>
                  <div className="provider-url">{p.type} · {p.base_url || '未填 Base URL'}</div>
                  <div className="model-row-actions">
                    <button onClick={() => {
                      setEditingProviderId(p.id)
                      setProviderForm({ name: p.name, type: p.type, base_url: p.base_url, api_key: '', auth_type: p.auth_type, extra_headers_text: JSON.stringify(p.extra_headers ?? {}, null, 2), enabled: p.enabled, note: p.note })
                    }}>编辑</button>
                    <button onClick={async () => { await updateModelProvider(p.id, { enabled: !p.enabled }); await load() }}>{p.enabled ? '禁用' : '启用'}</button>
                    <button className="danger" onClick={async () => { try { await deleteModelProvider(p.id); await load() } catch (e) { toast(e instanceof Error ? e.message : String(e)) } }}>删除</button>
                  </div>
                </div>
              ))}
              {!providers.length && <div className="model-empty">还没有 Provider，先添加一个 OpenAI 兼容服务。</div>}
            </div>

            <div className="model-form compact">
              <div className="form-title">{editingProviderId ? '编辑 Provider（Key 留空不覆盖）' : '添加 Provider'}</div>
              <input placeholder="名称，如 Example Provider / OpenAI-compatible / Local Model" value={providerForm.name} onChange={(e) => setProviderForm({ ...providerForm, name: e.target.value })} />
              <div className="form-grid two">
                <input placeholder="类型 openai_compatible" value={providerForm.type} onChange={(e) => setProviderForm({ ...providerForm, type: e.target.value })} />
                <input placeholder="鉴权 bearer / custom_headers" value={providerForm.auth_type} onChange={(e) => setProviderForm({ ...providerForm, auth_type: e.target.value })} />
              </div>
              <input placeholder="Base URL，如 https://api.openai.com/v1" value={providerForm.base_url} onChange={(e) => setProviderForm({ ...providerForm, base_url: e.target.value })} />
              <input placeholder={editingProviderId ? '新 API Key（留空不覆盖旧 Key）' : 'API Key'} type="password" value={providerForm.api_key} onChange={(e) => setProviderForm({ ...providerForm, api_key: e.target.value })} />
              <textarea placeholder='额外请求头 JSON，如 {"Example-Access-Client-Id":"..."}' value={providerForm.extra_headers_text} onChange={(e) => setProviderForm({ ...providerForm, extra_headers_text: e.target.value })} />
              <input placeholder="备注" value={providerForm.note} onChange={(e) => setProviderForm({ ...providerForm, note: e.target.value })} />
              <label className="check"><input type="checkbox" checked={providerForm.enabled} onChange={(e) => setProviderForm({ ...providerForm, enabled: e.target.checked })} /> 启用</label>
              <div className="form-actions"><button onClick={resetProvider}>清空</button><button className="primary" disabled={busy || !providerForm.name.trim()} onClick={saveProvider}>保存 Provider</button></div>
            </div>
          </section>

          <section className="model-panel wide">
            <div className="model-panel-title">模型配置</div>
            <div className="cap-grid">
              {grouped.map((g) => (
                <div key={g.value} className="cap-card">
                  <div className="cap-head">
                    <b>{g.label}</b><span>{g.hint}</span>
                    <span className={g.enabled.length ? 'state on' : 'state'}>{g.enabled.length ? `已配置 · ${g.enabled[0].model_name}` : '未配置'}</span>
                  </div>
                  {g.models.map((m) => {
                    const result = testResults[m.id]
                    return (
                      <div key={m.id} className="model-config-card">
                        <div className="provider-main"><b>{m.model_name}</b><span className={m.enabled ? 'state on' : 'state'}>{m.enabled ? '启用' : '禁用'}</span><code>优先级 {m.priority}</code></div>
                        <div className="provider-url">{m.provider_name ?? `Provider #${m.provider_id}`} · {m.note || '无备注'}</div>
                        {result && <div className={result.ok ? 'test-result ok' : 'test-result'}>{result.ok ? 'OK' : result.error_type} · {result.message}{result.latency_ms != null ? ` · ${result.latency_ms}ms` : ''}</div>}
                        <div className="model-row-actions">
                          <button disabled={testingId === m.id} onClick={() => void runTest(m)}>{testingId === m.id ? '测试中…' : '测试连接'}</button>
                          <button onClick={() => {
                            setEditingModelId(m.id)
                            setModelForm({ provider_id: m.provider_id, model_name: m.model_name, capability: m.capability, enabled: m.enabled, priority: m.priority, params_text: JSON.stringify(m.params ?? {}, null, 2), note: m.note })
                          }}>编辑</button>
                          <button onClick={async () => { await updateModelConfig(m.id, { enabled: !m.enabled }); await load() }}>{m.enabled ? '禁用' : '启用'}</button>
                          <button className="danger" onClick={async () => { await deleteModelConfig(m.id); await load() }}>删除</button>
                        </div>
                      </div>
                    )
                  })}
                  {!g.models.length && <div className="model-empty mini">未配置。添加并启用一个{g.label}模型后，对应能力才可使用。</div>}
                  {!!g.models.length && !g.enabled.length && <div className="model-empty mini">未配置：现有模型或 Provider 均未启用。</div>}
                </div>
              ))}
            </div>

            <div className="model-form">
              <div className="form-title">{editingModelId ? '编辑模型配置' : '添加模型配置'}</div>
              <div className="form-grid two">
                <select value={modelForm.provider_id} onChange={(e) => setModelForm({ ...modelForm, provider_id: Number(e.target.value) })}>
                  <option value={0}>选择 Provider</option>
                  {providers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
                <select value={modelForm.capability} onChange={(e) => setModelForm({ ...modelForm, capability: e.target.value as ModelCapability })}>
                  {CAPABILITIES.map((c) => <option key={c.value} value={c.value}>{c.label} · {c.value}</option>)}
                </select>
              </div>
              <div className="form-grid two">
                <input placeholder="模型名，如 chat-model / vision-model / image-model" value={modelForm.model_name} onChange={(e) => setModelForm({ ...modelForm, model_name: e.target.value })} />
                <input placeholder="优先级（越小越优先）" type="number" value={modelForm.priority} onChange={(e) => setModelForm({ ...modelForm, priority: Number(e.target.value) })} />
              </div>
              <textarea placeholder='参数 JSON，如 {"temperature":0.2}' value={modelForm.params_text} onChange={(e) => setModelForm({ ...modelForm, params_text: e.target.value })} />
              <input placeholder="备注" value={modelForm.note} onChange={(e) => setModelForm({ ...modelForm, note: e.target.value })} />
              <label className="check"><input type="checkbox" checked={modelForm.enabled} onChange={(e) => setModelForm({ ...modelForm, enabled: e.target.checked })} /> 启用</label>
              <div className="form-actions"><button onClick={resetModel}>清空</button><button className="primary" disabled={busy || !modelForm.provider_id || !modelForm.model_name.trim()} onClick={saveModel}>保存模型</button></div>
            </div>
          </section>
        </div>

        {guideOpen && (
          <div className="model-guide-mask" onClick={() => setGuideOpen(false)}>
            <section
              className="model-guide"
              role="dialog"
              aria-modal="true"
              aria-labelledby="model-guide-title"
              onClick={(e) => e.stopPropagation()}
            >
              <header className="model-guide-head">
                <div>
                  <h2 id="model-guide-title">模型与 API 配置教程</h2>
                  <p>按下面步骤配置 OpenAI Compatible 服务；示例均为占位符。</p>
                </div>
                <button type="button" aria-label="关闭配置教程" onClick={() => setGuideOpen(false)}>×</button>
              </header>

              <div className="model-guide-body">
                <section className="model-guide-section">
                  <h3>三类能力</h3>
                  <div className="model-guide-capabilities">
                    <article><b>推理 · chat</b><span>负责讲解、解题、总结与对话。</span></article>
                    <article><b>识图 · vision</b><span>读取扫描页、公式、图表和课件中的图形。</span></article>
                    <article><b>生图 · image_gen</b><span>生成复杂几何、实物或概念示意图。</span></article>
                  </div>
                  <p className="model-guide-note">程序化函数图、波形图、布尔/电路图可能优先走本地绘图；复杂示意图才调用生图模型。</p>
                </section>

                <section className="model-guide-section">
                  <h3>Provider 各字段怎么填</h3>
                  <dl className="model-guide-fields">
                    <div><dt>Provider</dt><dd>一组供应商连接信息；同一个 Provider 可以挂多个模型。</dd></div>
                    <div><dt>Base URL</dt><dd>供应商给出的 API 根地址，例如 <code>https://api.example.com/v1</code>。是否包含 <code>/v1</code> 以供应商文档为准。</dd></div>
                    <div><dt>API Key</dt><dd>填供应商创建的密钥，例如 <code>sk-...</code>。</dd></div>
                    <div><dt>鉴权方式</dt><dd>当前模型调用使用 <code>bearer</code>：API Key 会作为 Authorization Bearer Key 发送。网关要求的其他鉴权信息请放入额外请求头。</dd></div>
                    <div><dt>额外请求头</dt><dd>填写 JSON 对象，例如 <code>{'{"X-Client-Id":"..."}'}</code>；仅在供应商或网关明确要求时添加。</dd></div>
                  </dl>
                </section>

                <section className="model-guide-section">
                  <h3>完整配置步骤</h3>
                  <ol className="model-guide-steps">
                    <li><b>添加 Provider：</b>填写名称、类型 <code>openai_compatible</code>、Base URL、Key 与鉴权信息，保持启用后保存。</li>
                    <li><b>添加模型：</b>选择刚保存的 Provider，模型名填写供应商的精确标识，例如 <code>your-model-name</code>。</li>
                    <li><b>选择能力：</b>为模型选择推理、识图或生图；同一模型支持多种能力时，可分别添加多条配置。</li>
                    <li><b>设置优先级并启用：</b>数字越小越优先，同时确保模型与所属 Provider 都已启用。</li>
                    <li><b>测试连接：</b>保存后点击该模型的“测试连接”，根据返回的状态和耗时排查问题。</li>
                  </ol>
                  <p className="model-guide-note"><b>同一 Provider 多模型：</b>重复“添加模型配置”，每次选择同一 Provider，填写不同模型名、能力和优先级即可。</p>
                  <p className="model-guide-warning"><b>当前路由行为：</b>每类能力默认选择已启用项中 priority 最小的模型（相同则 id 最小者优先）。AI 对话栏会同步这些真实推理模型，你可以只为当前对话临时切换，不会改动识图/生图配置，也不会修改全局优先级。请求失败时会直接返回错误，不会在一次请求失败后自动尝试下一模型。</p>
                </section>

                <section className="model-guide-section">
                  <h3>常见错误</h3>
                  <ul className="model-guide-errors">
                    <li><b>401 / 403：</b>Key 无效、权限不足，或缺少供应商要求的额外请求头。</li>
                    <li><b>404 / model_not_found：</b>模型名不正确，或 Base URL 路径不匹配；模型名必须与供应商完全一致。</li>
                    <li><b>timeout / network：</b>服务不可达、网络受限、网关超时或供应商响应过慢。</li>
                    <li><b>Base URL：</b>有的服务要求包含 <code>/v1</code>，有的不要求，请以其 OpenAI Compatible 文档为准。</li>
                  </ul>
                </section>

                <section className="model-guide-section safety">
                  <h3>密钥安全</h3>
                  <p>保存后 Key 只显示掩码，额外请求头中的敏感值也会掩码。不要把 Key 提交到 Git、放进公开截图或发送给别人；编辑 Provider 时 Key 留空不会覆盖旧值。</p>
                </section>
              </div>

              <footer className="model-guide-foot">
                <span>按 Esc 也可关闭</span>
                <button type="button" onClick={() => setGuideOpen(false)}>知道了</button>
              </footer>
            </section>
          </div>
        )}
      </div>
    </div>
  )
}
