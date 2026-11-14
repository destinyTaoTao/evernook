/* Browser half: React is provided by dsh's module table, never bundled twice. */
window.__ModuleLoader__.load({ id: 'dsh-evernook', factory: (require) => {
  const React = require('react');
  const { createElement: h, useState, useEffect, useRef } = React;
  const ns = 'evernook';
  const zh = {
    title: '忆栖', subtitle: '让重要的话有据可寻', preview: '预览版', notice: '当前版本尚未通过正式日用验收，工具工作流与可靠流式仍有支持限制。', leaseNotice: '页面定期核验记忆；连接失效时隐藏内容。已交付的内容无法撤回。',
    privateOpen: '私密记录（本机窗口）', privateHelp: '独立记录，不属于当前聊天；请仅在本机窗口输入。', privateRequested: '已请求启动本机窗口；请在窗口中确认保存结果。',
    privateCheck: '查看窗口结果', privateRetry: '重试未确认的记录', privateAbandon: '放弃这次重试', privateWaiting: '窗口请求已启动；结束前不能确认保存。', privateSaved: '本机窗口确认已加密保存。', privateCanceled: '窗口已关闭，本次未提交。', privateUnknown: '保存结果不确定。重试时请在本机窗口输入完全相同的内容；不同内容会被拒绝。放弃重试会丢弃编号，原记录仍可能已经保存。', privateNever: '尚未请求私密记录。', privateFailed: '窗口启动失败。', privateId: '重试编号', privateRestart: '编号只保留在当前宿主进程，重启前请记下。',
    profiles: '我的画像', interests: '兴趣与偏好', timeline: '记忆时间线', network: '记忆关系', drafts: '待审草案', suppressed: '已移出画像', rejected: '已拒绝',
    networkHelp: '每层按槽位分页；共同来源连接本页关联记忆，结果并非全库。点击来源会重新检查可见性。', networkSource: '来源',
    graph: '跨层关系', graphEvidence: '依据', profile: '画像', fact: '事实', observation: '观察', topic: '主题视图', valid: '有效', conflict: '冲突',
    topic_settings: '专题设置', topicNew: '新建专题', topicEdit: '编辑专题', topicTitle: '标题', topicPurpose: '用途', topicQuery: '检索问题', topicEntries: '入口（每行一个，留空表示不限）', topicSessions: '会话范围（JSON 数组，每项为 [入口, 会话]）', topicSources: '来源 ID（每行一个）', topicObservations: '观察键（每行一个）', topicSince: '起始时间（ISO，含）', topicUntil: '结束时间（ISO，不含）', topicTTL: '有效秒数', topicInterval: '刷新间隔秒数', topicMaxSources: '最大来源数', topicMaxChars: '最大字符数', topicPolicy: '策略版本', topicPause: '暂停刷新', topicResume: '恢复刷新', topicDelete: '删除专题', topicConfirm: '确认更改专题', topicControlHelp: '只更改派生专题，不删除原始记录。操作结果不确定时请刷新核对，勿重复新建。', topicUnknown: '未能确认专题操作结果。请核对刷新后的列表；新建失败时也可能已保存。', topicInvalid: '请检查专题范围和数值格式。', topicPending: '待刷新', topicPaused: '已暂停',
    networkLayer: '记忆层', networkCategory: '类别标识', networkCategoryHelp: '输入完整类别标识（含空格），例如 interest、milestone；留空显示全部。类别筛选会排除没有类别的主题。', facts: '事实', observations: '综合观察', disputed: '存在争议', conflicted: '存在冲突', published: '已发布', current: '当前', unknown_time: '时间未知',
    topic_needs_refresh: '需要刷新', topic_expired: '已过期', topic_pending: '待刷新', topic_paused: '已暂停', topic_empty: '暂无依据', topicEmpty: '尚无专题', topicEmptyHelp: '创建专题后，可以配置范围和刷新策略。', topicDeleteHelp: '专题定义和快照将永久删除；原始记录保留。',
    inference: '推断', support: '支持依据', counterevidence: '反证', networkFilterUnavailable: '事实和观察层暂不支持搜索及分类筛选。', networkEmpty: '当前页没有可显示的记忆；如有下一页，可继续查看。',
    filterEntry: '入口', filterSession: '会话 ID', filterSince: '发生日期从（含）', filterUntil: '发生日期至（不含）', filterTimeHelp: '日期按本机时区；未提供发生时间的记录在日期筛选时不显示。', invalidTimeRange: '结束日期必须晚于起始日期。',
    diagnostics: '连接与配置诊断', diagnosticScope: '宿主配置与本次可信管理读取；不代表模型或日用能力已通过验收。', diagnosticSchema: 'Core 配置格式版本', diagnosticService: '管理服务', diagnosticOnline: '读取成功', diagnosticLoading: '检查中', diagnosticUnavailable: '读取失败或不可用', diagnosticStream: '宿主流模式', diagnosticRecovery: '只读续接', diagnosticEnabled: '可用', diagnosticDisabled: '未启用或不支持',
    diagnosticPrivate: '本机私密记录入口', diagnosticFingerprint: '指纹复核桥接', diagnosticConfigured: '已配置（未检查健康状态）', diagnosticNotConfigured: '未配置',
    diagnosticEmbeddingFormat: '嵌入配置格式', diagnosticContextFormat: '预算上下文配置格式', validationValid: '格式检查通过', validationInvalid: '配置无效或不可读', validationNone: '未配置', validationUnknown: '未知或旧版服务未提供', validationHelp: '格式检查不验证模型文件、运行健康或质量准入。',
    persona: '伴侣人设', persona_identity: '身份', persona_tone: '语气', persona_boundary: '边界', persona_address: '称呼', persona_state: '状态', personaIndependent: '人设由用户明确设定；撤回来源不会自动修改人设。页面修改需要先预览，再确认。', personaSourceUnavailable: '原设定来源已不可用或版本已变化。', personaEmpty: '尚未设置人设', personaNew: '设置人设', personaEdit: '修改人设', personaClear: '清空此分块', personaClearValue: '确认后清空此分块', personaBlock: '人设分块', personaUnsupported: '当前服务未提供人设编辑能力。', personaSecret: '人设属于普通记忆，请勿在此输入凭证或私密内容。', personaUnknown: '修改结果未能确认，请检查刷新后的人设；不要重复提交。',
    diagnosticEmbedding: '嵌入模型', diagnosticReflection: '复杂反思模型', diagnosticExtraction: '候选抽取模型', diagnosticSpeech: '语音', providerLocal: '已配置本地（未检查健康或准入）', providerRemote: '已配置线上（须单独授权，未检查健康或准入）', providerNone: '服务未配置', providerHost: '由宿主或独立语音适配器负责', providerUnknown: '未知或旧版服务未提供',
    local: '本地处理', evidence: '每条都有来源', control: '随时纠正与撤回', refresh: '刷新', search: '搜索当前分类', searchGo: '搜索',
    all: '全部分类', identity: '身份', preference: '偏好', interest: '兴趣', relation: '关系', goal: '目标', boundary: '边界',
    empty: '这里还没有记忆', emptyHelp: '已有原话不等于已经形成画像。可以先查看时间线，或到待审草案确认有来源的内容。',
    loading: '正在读取记忆…', next: '下一页', previous: '上一页', currentPage: '当前页', source: '查看来源', revise: '更正',
    remove: '移出画像', suppress: '停止召回来源', accept: '接纳草案', reject: '拒绝草案', reopen: '重新审查', history: '版本历史',
    close: '关闭', cancel: '取消', confirm: '确认此操作', review: '请核对变更', old: '原内容', replacement: '新内容',
    originalRetained: '原始档案会保留；已经交付给宿主或模型的内容无法撤回。',
    removeHelp: '移出画像后，该标签不再作为当前画像使用，原话仍可单独检索。',
    suppressHelp: '所选来源和依赖它的记忆将退出普通召回。其他独立来源及同义表述不在本次范围内。',
    rejectHelp: '同一来源版本的同一候选不会因补跑或模型升级再次提示。原话仍可检索。',
    reviseHelp: '更正作为新的用户证据保存，旧版本与原话保留。', affected: '已识别的关联记忆（主题视图可能另受影响）', noDerived: '没有列出的画像、事实、观察或经验依赖',
    sources: '依据', partial: '此处为片段，完整来源可分页查看', more: '继续读取', occurred: '发生时间', received: '记录时间', unknown: '未提供',
    saved: '操作已保存', changed: '内容已变化，请重新预览', expired: '预览已过期，请重新打开', unavailable: '记忆暂不可用，请稍后刷新',
    forbidden: '连接已失效或无权访问，请重新加载页面', config: '尚未配置记忆连接', configHelp: '请在插件配置中填写本地 Python、记忆配置、采集身份和独立维护身份。配置前不会采集或修改记忆。',
    session: '会话记忆', sessionHelp: '会话记录只显示当前会话；用户画像在已授权入口之间共享。', status: '采集状态',
    budgetMode: '记忆预算', budgetTarget: '目标分词器模式', budgetLegacy: '字符兼容模式（不是真实 token 预算）', budgetUnknown: '未提供预算状态',
    gaps: '采集缺口', noGaps: '当前没有报告采集缺口', raw: '普通原话', projection: '脱敏投影', user: '用户', assistant: '助手', tool: '工具',
    draft: '待审', confirmed: '已接纳', version: '版本', uncertain: '时间或推断不等于已确认事实',
    textOnly: '可靠文本流模式：工具、推理块与多文本块尚不支持', buffered: '整段保存后显示：通用工具结果发布屏障尚未验收',
    bufferedTools: '整段保存后显示，工具调用和结果也须先取得保存回执；正式日用仍待验收',
    durableTools: '可靠文本与工具模式：安全文本先保存再显示；工具与顺序推理等待完整保存后展示。暂不支持附件，正式日用仍待验收',
    recovery: '核对工具发布', recoveryHelp: '旧会话继续锁定。这里不重跑工具、不补造结果。核对后只创建禁止工具执行的新会话，用于阅读和讨论证据。',
    recoveryUnknown: '副作用仍可能未知；已保存的工具报告不等于外部状态已独立确认。', recoveryNone: '当前没有待核对的工具发布',
    recoveryPreview: '预览只读续接', recoveryConfirm: '已核对，创建只读续接', recoveryCreated: '只读续接已保存', recoveryOpen: '打开只读会话',
    recoveryComplete: '完整结果回执', recoveryPartial: '结果有保存缺口', recoveryUnconfirmed: '结果保存未确认', recoveryReadOnly: '只读续接：工具执行被宿主禁止',
    recoveryInput: '原始用户输入', recoveryCall: '调用参数', recoveryResult: '工具报告', recoveryPtcCall: 'PTC 调用', recoveryPtcLog: 'PTC 日志',
    encryption: '普通记忆未做全库加密，凭证不通过本页面展示', sourceUnavailable: '该来源已不可读取，可能已更正或停止召回',
    clarification: '此项需要专门的冲突澄清，不能作为普通草案直接接纳', page: '页', result: '实际结果',
  };
  const en = {
    title: 'EverNook', subtitle: 'Important words, backed by evidence', preview: 'Preview', notice: 'Daily-use admission is pending. Tool workflows and reliable streaming have explicit limitations.', leaseNotice: 'Visible memories are rechecked periodically and hidden if the connection fails. Previously delivered content cannot be recalled.',
    privateOpen: 'Private record (local window)', privateHelp: 'A separate record, not this chat. Enter it only in the local window.', privateRequested: 'Local window launch requested; confirm the save result in the window.',
    privateCheck: 'Check window result', privateRetry: 'Retry unconfirmed record', privateAbandon: 'Abandon this retry', privateWaiting: 'Window requested; the save is not confirmed until it finishes.', privateSaved: 'The local window confirmed encrypted storage.', privateCanceled: 'Window closed without submission.', privateUnknown: 'The save outcome is unknown. Retry with exactly the same text in the local window; different text is rejected. Abandoning discards the IDs; the record may already exist.', privateNever: 'No private record requested.', privateFailed: 'Window launch failed.', privateId: 'Retry IDs', privateRestart: 'IDs are held only by this host process; note them before restarting.',
    profiles: 'Your profile', interests: 'Interests & preferences', timeline: 'Memory timeline', network: 'Memory links', drafts: 'Review drafts', suppressed: 'Removed from profile', rejected: 'Rejected',
    networkHelp: 'Each layer is paged by slot. Shared sources link memories on this page; this is not the full database. Opening a source checks visibility again.', networkSource: 'Source',
    graph: 'Cross-layer links', graphEvidence: 'Evidence', profile: 'Profile', fact: 'Fact', observation: 'Observation', topic: 'Topic view', valid: 'Valid', conflict: 'Conflict',
    topic_settings: 'Topic settings', topicNew: 'New topic', topicEdit: 'Edit topic', topicTitle: 'Title', topicPurpose: 'Purpose', topicQuery: 'Search question', topicEntries: 'Entries (one per line; empty means all)', topicSessions: 'Sessions (JSON array of [entry, session] pairs)', topicSources: 'Source IDs (one per line)', topicObservations: 'Observation keys (one per line)', topicSince: 'From time (ISO, inclusive)', topicUntil: 'Until time (ISO, exclusive)', topicTTL: 'Lifetime in seconds', topicInterval: 'Refresh interval in seconds', topicMaxSources: 'Maximum sources', topicMaxChars: 'Maximum characters', topicPolicy: 'Policy version', topicPause: 'Pause refresh', topicResume: 'Resume refresh', topicDelete: 'Delete topic', topicConfirm: 'Confirm topic change', topicControlHelp: 'Only the derived topic changes; original records remain. If the result is uncertain, refresh before creating another.', topicUnknown: 'The topic change could not be confirmed. Check the refreshed list; a new topic may already have been saved.', topicInvalid: 'Check the topic scope and numeric fields.', topicPending: 'Pending refresh', topicPaused: 'Paused',
    networkLayer: 'Memory layer', networkCategory: 'Category identifier', networkCategoryHelp: 'Enter the exact category identifier, including spaces, such as interest or milestone; leave empty for all. Topics have no category and are excluded when filtering.', facts: 'Facts', observations: 'Observations', disputed: 'Disputed', conflicted: 'Conflicted', published: 'Published', current: 'Current', unknown_time: 'Time unknown',
    topic_needs_refresh: 'Needs refresh', topic_expired: 'Expired', topic_pending: 'Pending', topic_paused: 'Paused', topic_empty: 'No evidence', topicEmpty: 'No topics yet', topicEmptyHelp: 'Create a topic to configure its scope and refresh policy.', topicDeleteHelp: 'The topic definition and snapshots will be permanently deleted; original records remain.',
    inference: 'Inference', support: 'Supporting evidence', counterevidence: 'Counterevidence', networkFilterUnavailable: 'Search and category filters are not yet available for facts or observations.', networkEmpty: 'No visible memories on this page. Continue if a next page is available.',
    filterEntry: 'Entry', filterSession: 'Session ID', filterSince: 'Occurred from (inclusive)', filterUntil: 'Occurred before (exclusive)', filterTimeHelp: 'Dates use the local time zone. Records without occurrence time are excluded when dates are set.', invalidTimeRange: 'The end date must be later than the start date.',
    diagnostics: 'Connection and configuration', diagnosticScope: 'Host configuration and this trusted management read; not model or daily-use admission.', diagnosticSchema: 'Core configuration schema', diagnosticService: 'Management service', diagnosticOnline: 'Read succeeded', diagnosticLoading: 'Checking', diagnosticUnavailable: 'Read failed or unavailable', diagnosticStream: 'Host stream mode', diagnosticRecovery: 'Read-only continuation', diagnosticEnabled: 'Available', diagnosticDisabled: 'Disabled or unsupported',
    diagnosticPrivate: 'Local private record entry', diagnosticFingerprint: 'Fingerprint revalidation bridge', diagnosticConfigured: 'Configured (health not checked)', diagnosticNotConfigured: 'Not configured',
    diagnosticEmbeddingFormat: 'Embedding configuration format', diagnosticContextFormat: 'Budgeted context configuration format', validationValid: 'Format check passed', validationInvalid: 'Configuration invalid or unreadable', validationNone: 'Not configured', validationUnknown: 'Unknown or unavailable in older service', validationHelp: 'A format check does not verify model files, runtime health or quality admission.',
    persona: 'Companion persona', persona_identity: 'Identity', persona_tone: 'Tone', persona_boundary: 'Boundary', persona_address: 'Address', persona_state: 'State', personaIndependent: 'Persona is explicitly set by the user; withdrawing its source does not change it. Changes here require a preview and confirmation.', personaSourceUnavailable: 'The original setting source is unavailable or has changed.', personaEmpty: 'No persona configured', personaNew: 'Set persona', personaEdit: 'Edit persona', personaClear: 'Clear this block', personaClearValue: 'This block will be cleared on confirmation', personaBlock: 'Persona block', personaUnsupported: 'This service does not offer persona editing.', personaSecret: 'Persona is ordinary memory. Do not enter credentials or private material here.', personaUnknown: 'The change could not be confirmed. Check the refreshed persona before trying again.',
    diagnosticEmbedding: 'Embedding model', diagnosticReflection: 'Reflection model', diagnosticExtraction: 'Candidate extraction model', diagnosticSpeech: 'Speech', providerLocal: 'Local configured (health and admission not checked)', providerRemote: 'Remote configured (separate consent required; health and admission not checked)', providerNone: 'Not configured in service', providerHost: 'Owned by host or separate speech adapter', providerUnknown: 'Unknown or unavailable in older service',
    local: 'Processed locally', evidence: 'Linked to evidence', control: 'Correct or withdraw', refresh: 'Refresh', search: 'Search this category', searchGo: 'Search',
    all: 'All categories', identity: 'Identity', preference: 'Preference', interest: 'Interest', relation: 'Relationship', goal: 'Goal', boundary: 'Boundary',
    empty: 'No memories here yet', emptyHelp: 'Saved words do not automatically become profile facts. Browse the timeline or review source-backed drafts.',
    loading: 'Loading memories…', next: 'Next', previous: 'Previous', currentPage: 'This page', source: 'View source', revise: 'Correct',
    remove: 'Remove from profile', suppress: 'Stop recalling source', accept: 'Accept draft', reject: 'Reject draft', reopen: 'Review again', history: 'Version history',
    close: 'Close', cancel: 'Cancel', confirm: 'Confirm this change', review: 'Review the change', old: 'Previous', replacement: 'Replacement',
    originalRetained: 'Original records remain. Content already delivered to the host or model cannot be retracted.',
    removeHelp: 'This tag will leave the current profile. Its original words remain searchable.',
    suppressHelp: 'This source and dependent memories leave ordinary recall. Independent sources and paraphrases are outside this scope.',
    rejectHelp: 'The same candidate from the same source version will not prompt again after reprocessing or a model update. Original words remain searchable.',
    reviseHelp: 'Your correction is saved as new user evidence. Previous versions and original words remain.', affected: 'Identified linked memories (topic views may also be affected)', noDerived: 'No listed profile, fact, observation, or experience dependencies',
    sources: 'Evidence', partial: 'Excerpt only. Read the full source in pages.', more: 'Read more', occurred: 'Occurred', received: 'Recorded', unknown: 'Not provided',
    saved: 'Change saved', changed: 'Content changed. Open a new preview.', expired: 'Preview expired. Open it again.', unavailable: 'Memory is unavailable. Please refresh later.',
    forbidden: 'Connection expired or access denied. Reload this page.', config: 'Memory connection is not configured', configHelp: 'Set the local Python, memory configuration, capture identity, and separate maintenance identity in plugin settings. No memory is captured or changed before configuration.',
    session: 'Session memory', sessionHelp: 'Records are scoped to this session. Your profile is shared between authorized entry points.', status: 'Capture status',
    budgetMode: 'Memory budget', budgetTarget: 'Target tokenizer mode', budgetLegacy: 'Character compatibility mode (not a token budget)', budgetUnknown: 'Budget status unavailable',
    gaps: 'Capture gaps', noGaps: 'No capture gaps currently reported', raw: 'Original words', projection: 'Redacted projection', user: 'User', assistant: 'Assistant', tool: 'Tool',
    draft: 'Draft', confirmed: 'Accepted', version: 'Version', uncertain: 'A timestamp or inference is not a confirmed fact',
    textOnly: 'Reliable text mode: tools, reasoning, and multiple text blocks are unsupported', buffered: 'Saved before display: the general tool-result publication barrier is unverified',
    bufferedTools: 'Buffered display with saved tool calls and results. Daily-use acceptance is pending.',
    durableTools: 'Saved text streams live; tools and sequential reasoning wait for complete storage before display. Attachments are unsupported. Daily-use acceptance is pending.',
    recovery: 'Review tool publication', recoveryHelp: 'The original session stays blocked. No tool retries or invented results. Confirmation only creates a new tool-disabled session for evidence review and discussion.',
    recoveryUnknown: 'Side effects may remain unknown. A saved tool report is not independent confirmation of external state.', recoveryNone: 'No unresolved tool publication in this session',
    recoveryPreview: 'Preview read-only continuation', recoveryConfirm: 'Reviewed: create read-only continuation', recoveryCreated: 'Read-only continuation saved', recoveryOpen: 'Open read-only session',
    recoveryComplete: 'Complete result receipt', recoveryPartial: 'Incomplete result capture', recoveryUnconfirmed: 'Result storage unconfirmed', recoveryReadOnly: 'Read-only continuation: the host disables tool execution',
    recoveryInput: 'Original user input', recoveryCall: 'Call arguments', recoveryResult: 'Tool report', recoveryPtcCall: 'PTC call', recoveryPtcLog: 'PTC log',
    encryption: 'Ordinary memory is not fully encrypted at rest. This page never displays vault credentials.', sourceUnavailable: 'Source unavailable. It may have been corrected or suppressed.',
    clarification: 'This item needs the dedicated conflict-clarification flow, not ordinary acceptance.', page: 'Page', result: 'Actual result',
  };
  const css = `
    .en-mem{--en-accent:#377458;--en-soft:color-mix(in srgb,currentColor 4%,transparent);font:inherit;color:inherit;box-sizing:border-box;height:100%;overflow:auto;padding:28px;line-height:1.55}
    .dark .en-mem{--en-accent:#9acdb0}.en-mem *{box-sizing:border-box}.en-wrap{max-width:1120px;margin:0 auto}.en-head{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:22px}
    .en-brand{display:flex;gap:12px;align-items:center}.en-mark{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;color:var(--en-accent);background:color-mix(in srgb,var(--en-accent) 13%,transparent);font-size:27px}
    .en-mem h1{font-size:24px;font-weight:650;letter-spacing:-.5px;margin:0}.en-mem h2{font-size:16px;margin:0 0 10px}.en-muted{opacity:.62;font-size:12px}.en-pill{font-size:11px;padding:3px 8px;border:1px solid color-mix(in srgb,currentColor 15%,transparent);border-radius:20px;white-space:nowrap}
    .en-notice{padding:12px 15px;border-left:3px solid var(--en-accent);background:var(--en-soft);font-size:12px;margin-bottom:20px;border-radius:0 9px 9px 0}.en-values{display:flex;gap:22px;flex-wrap:wrap;margin-bottom:22px;color:var(--en-accent);font-size:12px}
    .en-tabs{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid color-mix(in srgb,currentColor 13%,transparent);padding-bottom:10px;margin-bottom:20px}.en-mem button,.en-mem input,.en-mem select,.en-mem textarea{font:inherit;color:inherit}
    .en-mem button{border:1px solid color-mix(in srgb,currentColor 16%,transparent);background:transparent;border-radius:7px;padding:7px 11px;font-size:12px;cursor:pointer}.en-mem button:hover{background:var(--en-soft)}.en-mem button:disabled{opacity:.4;cursor:wait}
    .en-mem button[aria-selected=true],.en-mem .en-primary,.en-mem .en-primary:hover,.en-mem button[aria-selected=true]:hover{background:var(--en-accent);border-color:var(--en-accent);color:white}.dark .en-mem .en-primary,.dark .en-mem button[aria-selected=true]{color:#13251c}
    .en-mem button:focus-visible,.en-mem input:focus-visible,.en-mem textarea:focus-visible,.en-mem select:focus-visible{outline:2px solid var(--en-accent);outline-offset:3px}
    .en-toolbar{display:flex;gap:8px;margin-bottom:20px;align-items:center;flex-wrap:wrap}.en-mem input,.en-mem select,.en-mem textarea{background:var(--en-soft);border:1px solid color-mix(in srgb,currentColor 15%,transparent);border-radius:7px;padding:8px 11px;min-width:0;font-size:13px}.en-mem input{flex:1;min-width:130px}
    .en-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,290px),1fr));gap:12px}.en-card{border:1px solid color-mix(in srgb,currentColor 13%,transparent);border-radius:12px;padding:17px;background:color-mix(in srgb,currentColor 1%,transparent);overflow-wrap:anywhere}.en-card header{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:12px}.en-label{font-size:16px;font-weight:550;margin:8px 0 16px;white-space:pre-wrap;overflow-wrap:anywhere}.en-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:14px}
    .en-network{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:14px}.en-network-cluster{position:relative;overflow:hidden;border:1px solid color-mix(in srgb,#7c92f8 35%,transparent);border-radius:18px;padding:18px;background:radial-gradient(circle at 90% 0%,color-mix(in srgb,#7c92f8 18%,transparent),transparent 55%),var(--en-soft)}.en-network-node{position:relative;z-index:1;border-radius:14px;padding:14px;background:color-mix(in srgb,#7c92f8 18%,transparent);overflow-wrap:anywhere}.en-network-node p{margin:8px 0 0;white-space:pre-wrap}.en-network-edges{position:relative;margin:16px 0 0 18px;padding:0 0 0 20px;border-left:1px solid color-mix(in srgb,#7c92f8 70%,transparent);display:flex;flex-wrap:wrap;gap:8px}.en-network-edges:before{content:'';position:absolute;left:-1px;top:-16px;height:16px;border-left:1px solid color-mix(in srgb,#7c92f8 70%,transparent)}.en-network-edges button{position:relative;border-radius:999px}.en-network-edges button:before{content:'';position:absolute;left:-21px;top:50%;width:20px;border-top:1px solid color-mix(in srgb,#7c92f8 70%,transparent)}
    .en-empty{padding:55px 20px;text-align:center;border:1px dashed color-mix(in srgb,currentColor 20%,transparent);border-radius:12px}.en-empty p{max-width:440px;margin:12px auto;font-size:13px;opacity:.65}.en-page{display:flex;align-items:center;justify-content:flex-end;gap:12px;margin:18px 0}.en-meta{font-size:11px;opacity:.6;display:flex;gap:10px;flex-wrap:wrap}
    .en-error{padding:12px;border:1px solid #c66c62;border-radius:8px;margin:10px 0;font-size:13px}.en-success{padding:12px;background:color-mix(in srgb,var(--en-accent) 12%,transparent);border-radius:8px;margin-bottom:12px;font-size:13px}.en-rows{display:grid;gap:10px}
    .en-mem dialog{color:var(--dsw-alias-label-primary,#1d2220);background:var(--dsw-alias-bg-module-platform,#fff);border:1px solid #8885;border-radius:14px;width:min(720px,92vw);max-height:86vh;padding:24px;box-shadow:0 20px 70px #0003;overflow:auto}.en-mem dialog::backdrop{background:#0006}.en-mem dialog header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:10px}
    .en-topic-field{display:flex;flex-direction:column;gap:5px;margin:12px 0}.en-topic-field textarea{min-height:70px}
    .en-mem pre{font:inherit;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0;background:var(--en-soft);border-radius:8px;padding:14px}.en-mem textarea{width:100%;min-height:110px;resize:vertical}.en-mem .en-footer{display:flex;justify-content:flex-end;gap:8px;margin-top:22px}.en-side{padding:16px}.en-side .en-head{margin-bottom:12px}.en-side .en-grid{grid-template-columns:1fr}.en-side .en-values{display:none}.en-side h1{font-size:19px}.en-side .en-tabs{gap:6px}.en-side .en-notice{font-size:11px}
    @media(max-width:650px){.en-mem{padding:16px}.en-head{align-items:flex-start}.en-values{gap:12px}.en-mem dialog{padding:18px}}
  `;
  function apply(ctx) {
    ctx.effect(() => ctx.locale.register(ns, 'zh', zh));
    ctx.effect(() => ctx.locale.register(ns, 'en', en));
    const t = ctx.locale.bind(ns);
    let bootstrap;
    let active = true;
    const resources = [];
    const own = resource => {
      if (typeof resource === 'function') resources.push(resource);
      else if (resource && typeof resource.dispose === 'function') resources.push(() => resource.dispose());
    };
    const updates = new EventTarget();
    async function call(payload, signal) {
      if (!active) throw new Error('adapter_closed');
      const send = (body, token, requestSignal) => fetch('/api/evernook/manage', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...(token ? { 'X-EverNook-Csrf': token } : {}) }, body: JSON.stringify(body), signal: requestSignal });
      if (!bootstrap) bootstrap = send({ action: 'bootstrap' }, null, AbortSignal.timeout(10000))
        .then(async response => { if (!response.ok) throw new Error('forbidden'); return response.json(); })
        .catch(error => { bootstrap = undefined; throw error; });
      const session = await bootstrap;
      if (payload.action === 'bootstrap') return session;
      const response = await send(payload, session.csrf, signal ?? AbortSignal.timeout(35000));
      const body = await response.json();
      if (!response.ok) { if (response.status === 403) bootstrap = undefined; throw new Error(body.error ?? 'management_unavailable'); }
      return body.result;
    }
    const errorText = code => t(['management_preview_stale', 'recovery_stale', 'topic_view_revision_conflict'].includes(code) ? 'changed' : ['management_preview_expired', 'recovery_expired'].includes(code) ? 'expired'
      : code === 'forbidden' ? 'forbidden' : code === 'source_unavailable' ? 'sourceUnavailable'
      : code === 'invalid_topic_form' ? 'topicInvalid' : code === 'protected_persona' ? 'personaSecret'
      : code === 'invalid_time_range' ? 'invalidTimeRange' : code === 'preference_clarification_required' ? 'clarification' : 'unavailable');
    const time = value => value ? new Date(value).toLocaleString() : t('unknown');
    const button = (label, action, props = {}) => h('button', { type: 'button', onClick: action, ...props }, label);
    function Modal({ title, children, close, busy = false, error }) {
      const ref = useRef(null);
      useEffect(() => { const dialog = ref.current; dialog.showModal(); return () => dialog.close(); }, []);
      return h('dialog', { ref, 'aria-label': title, onCancel: event => { event.preventDefault(); if (!busy) close(); } },
        h('header', null, h('h2', null, title), button(t('close'), close, { disabled: busy })),
        error ? h('div', { className: 'en-error', role: 'alert' }, error) : null, children);
    }
    function MemoryPage({ sessionId, host }) {
      const [enabled, setEnabled] = useState(active);
      const [tab, setTab] = useState(host === 'sidebar' ? 'timeline' : 'profiles');
      const [networkLayer, setNetworkLayer] = useState('graph');
      const [category, setCategory] = useState('');
      const [networkCategoryInput, setNetworkCategoryInput] = useState('');
      const [query, setQuery] = useState('');
      const [filter, setFilter] = useState('');
      const [timelineInput, setTimelineInput] = useState({ entry: '', session: '', since: '', until: '' });
      const [timelineFilter, setTimelineFilter] = useState({ entry: '', session: '', since: '', until: '' });
      const [offsets, setOffsets] = useState([0]);
      const [data, setData] = useState({ items: [] });
      const [loading, setLoading] = useState(true);
      const [configured, setConfigured] = useState(null);
      const [contextMode, setContextMode] = useState(null);
      const [diagnostic, setDiagnostic] = useState(null);
      const [streamMode, setStreamMode] = useState(null);
      const [error, setError] = useState('');
      const [notice, setNotice] = useState('');
      const [revision, setRevision] = useState(0);
      const [sessionStatus, setSessionStatus] = useState(null);
      const [recovery, setRecovery] = useState(null);
      const [recoverySupported, setRecoverySupported] = useState(false);
      const [privateCapture, setPrivateCapture] = useState(null);
      const [privateResult, setPrivateResult] = useState(null);
      const [fingerprintBridge, setFingerprintBridge] = useState(null);
      const [currentEntry, setCurrentEntry] = useState('dsh');
      const [review, setReview] = useState(null);
      const [editing, setEditing] = useState(null);
      const [topicEdit, setTopicEdit] = useState(null);
      const [topicControl, setTopicControl] = useState(null);
      const [source, setSource] = useState(null);
      const [history, setHistory] = useState(null);
      const [busy, setBusy] = useState(false);
      const alive = useRef(true);
      const controllers = useRef(new Set());
      const readEpoch = useRef(0);
      const leases = useRef(new Map());
      const leaseTimer = useRef(null);
      const leaseAbort = useRef(null);
      const changeStreak = useRef(0);
      const now = () => ({ wall: Date.now(), mono: performance.now() });
      const remainingLease = lease => Math.min(lease.wall - Date.now(), lease.mono - performance.now());
      const invalidateReads = () => {
        readEpoch.current += 1; for (const lease of leases.current.values()) clearTimeout(lease.watchdog); leases.current.clear();
        if (leaseTimer.current !== null) clearTimeout(leaseTimer.current);
        leaseTimer.current = null; leaseAbort.current?.abort(); leaseAbort.current = null;
        setSource(null); setHistory(null);
      };
      const refresh = (resetStreak = true) => { if (resetStreak) changeStreak.current = 0; invalidateReads(); setData({ items: [] }); setLoading(true); setRevision(value => value + 1); setReview(null); setEditing(null); setTopicEdit(null); setTopicControl(null); };
      const expire = () => {
        invalidateReads(); setData({ items: [] }); setReview(null); setEditing(null); setTopicEdit(null); setTopicControl(null); setLoading(false); setError(t('unavailable'));
      };
      function armLease(slot, lease) {
        if (lease.watchdog !== undefined) clearTimeout(lease.watchdog);
        lease.watchdog = setTimeout(() => {
          if (leases.current.get(slot) !== lease || !alive.current) return;
          if (remainingLease(lease) > 0) { armLease(slot, lease); return; }
          expire();
        }, Math.max(0, remainingLease(lease)));
      }
      function dropLease(slot) {
        const lease = leases.current.get(slot);
        if (lease) clearTimeout(lease.watchdog);
        leases.current.delete(slot);
      }
      function scheduleLease() {
        if (!alive.current || leaseTimer.current !== null || leaseAbort.current || !leases.current.size) return;
        const epoch = readEpoch.current;
        const delay = Math.max(0, Math.min(3000, ...[...leases.current.values()].map(remainingLease)));
        leaseTimer.current = setTimeout(async () => {
          leaseTimer.current = null;
          if (!alive.current || epoch !== readEpoch.current || !leases.current.size) return;
          const snapshot = [...leases.current.entries()];
          const remaining = Math.min(...snapshot.map(([, lease]) => remainingLease(lease)));
          if (remaining <= 0) { expire(); return; }
          const controller = new AbortController(); leaseAbort.current = controller;
          let timeout;
          try {
            const deadline = new Promise((_, reject) => { timeout = setTimeout(() => { controller.abort(); reject(new Error('lease_expired')); }, remaining); });
            const checkedAt = now();
            const checks = call({ action: 'fingerprints', requests: snapshot.map(([, lease]) => lease.parameters) }, controller.signal)
              .then(result => {
                if (!result || !Array.isArray(result.tokens) || result.tokens.length !== snapshot.length
                    || result.tokens.some(token => typeof token !== 'string' || !/^[a-f0-9]{64}$/.test(token))) throw new Error('management_unavailable');
                return snapshot.map(([slot, lease], index) => ({ slot, lease, result: { token: result.tokens[index] } }));
              });
            const results = await Promise.race([checks, deadline]);
            if (!alive.current || epoch !== readEpoch.current) return;
            if (snapshot.some(([, lease]) => remainingLease(lease) <= 0)) { expire(); return; }
            for (const { slot, lease, result } of results) {
              if (leases.current.get(slot) !== lease) continue;
              if (!result || result.token !== lease.token) {
                changeStreak.current += 1;
                if (changeStreak.current > 3) expire(); else refresh(false);
                return;
              }
              lease.wall = checkedAt.wall + 12000; lease.mono = checkedAt.mono + 12000;
              armLease(slot, lease);
            }
            changeStreak.current = 0;
          } catch {
            if (alive.current && epoch === readEpoch.current) expire();
            return;
          } finally {
            if (timeout !== undefined) clearTimeout(timeout);
            if (leaseAbort.current === controller) leaseAbort.current = null;
          }
          scheduleLease();
        }, delay);
      }
      async function versionedRead(parameters, signal, slot) {
        const epoch = readEpoch.current, started = now();
        const response = await call({ ...parameters, action: 'read-versioned' }, signal);
        if (signal.aborted || epoch !== readEpoch.current) throw new Error('view_changed');
        if (!response || !/^[a-f0-9]{64}$/.test(response.token) || !response.value || Date.now() - started.wall >= 12000 || performance.now() - started.mono >= 12000) throw new Error('management_unavailable');
        if (slot) { dropLease(slot); const lease = { parameters, token: response.token, wall: started.wall + 12000, mono: started.mono + 12000 }; leases.current.set(slot, lease); armLease(slot, lease); scheduleLease(); }
        return response.value;
      }
      useEffect(() => {
        alive.current = true;
        const listener = () => refresh();
        const dispose = () => { invalidateReads(); setEnabled(false); setData({ items: [] }); setReview(null); setTopicEdit(null); setTopicControl(null);
          for (const controller of controllers.current) controller.abort(); };
        updates.addEventListener('changed', listener);
        updates.addEventListener('disposed', dispose);
        window.addEventListener('focus', listener);
        const visibility = () => { if (window.document?.visibilityState === 'hidden') expire(); else refresh(); };
        window.document?.addEventListener('visibilitychange', visibility);
        return () => { alive.current = false; readEpoch.current += 1; for (const lease of leases.current.values()) clearTimeout(lease.watchdog); leases.current.clear(); if (leaseTimer.current !== null) clearTimeout(leaseTimer.current); leaseAbort.current?.abort();
          updates.removeEventListener('changed', listener); updates.removeEventListener('disposed', dispose); window.removeEventListener('focus', listener); window.document?.removeEventListener('visibilitychange', visibility);
          for (const controller of controllers.current) controller.abort(); };
      }, []);
      useEffect(() => {
        if (!enabled) return;
        const controller = new AbortController();
        const epoch = readEpoch.current;
        setLoading(true); setError(''); setData({ items: [] }); setSessionStatus(null); setContextMode(null); setDiagnostic(null);
        (async () => {
          const boot = await call({ action: 'bootstrap' }, controller.signal);
          if (controller.signal.aborted || epoch !== readEpoch.current) return;
          setConfigured(boot.configured);
          setContextMode(boot.context_mode);
          setStreamMode(boot.stream_mode);
          setRecoverySupported(boot.recovery_supported === true); setCurrentEntry(boot.entry);
          setPrivateCapture(boot.private_capture === true);
          setFingerprintBridge(boot.fingerprint_bridge === true);
          if (!boot.configured) return;
          call({ action: 'read', view: 'status' }, controller.signal)
            .then(status => { if (!controller.signal.aborted && epoch === readEpoch.current) setDiagnostic({ available: true, schema: status.configuration_schema_version, models: status.model_configuration, validation: status.configuration_validation, actions: status.supported_actions }); })
            .catch(() => { if (!controller.signal.aborted && epoch === readEpoch.current) setDiagnostic({ available: false }); });
          if (host === 'sidebar' && tab === 'timeline' && !sessionId) throw new Error('invalid_session');
          const result = await versionedRead({ view: tab === 'network' ? 'network_' + networkLayer : tab,
            offset: offsets[offsets.length - 1], limit: 24, query: filter,
            category: category || null,
            session_id: tab === 'timeline' || tab === 'network' ? (host === 'sidebar' && tab === 'timeline' ? sessionId ?? null : timelineFilter.session || null) : null,
            ...(tab === 'timeline' || tab === 'network' ? { entry: timelineFilter.entry || null,
              since: timelineFilter.since ? new Date(timelineFilter.since + 'T00:00:00').toISOString() : null,
              until: timelineFilter.until ? new Date(timelineFilter.until + 'T00:00:00').toISOString() : null } : {}) }, controller.signal, 'page');
          if (!controller.signal.aborted && epoch === readEpoch.current) setData(tab === 'network' && networkLayer === 'graph' ? { ...result, items: result.nodes } : result);
          if (sessionId) {
            const status = await call({ action: 'session', session_id: sessionId }, controller.signal);
            if (!controller.signal.aborted && epoch === readEpoch.current) setSessionStatus(status);
          }
        })().catch(cause => { if (!controller.signal.aborted && epoch === readEpoch.current) setError(errorText(cause.message)); })
          .finally(() => { if (!controller.signal.aborted && epoch === readEpoch.current) setLoading(false); });
        return () => controller.abort();
      }, [tab, networkLayer, category, filter, timelineFilter.entry, timelineFilter.session, timelineFilter.since, timelineFilter.until, offsets.join(','), revision, sessionId, host, enabled]);
      async function perform(action) {
        if (busy) return;
        setBusy(true); setError('');
        const controller = new AbortController(); controllers.current.add(controller);
        try { await action(controller.signal); }
        catch (cause) { if (alive.current && !controller.signal.aborted) setError(errorText(cause.message)); }
        finally { controllers.current.delete(controller); if (alive.current) setBusy(false); }
      }
      function editTopic(item) {
        const definition = item?.definition ?? { title: '', purpose: '', query: '', entries: [], sessions: [], source_ids: [], observation_keys: [], since: null, until: null,
          ttl_seconds: 86400, refresh_interval_seconds: 5, max_sources: 16, max_chars: 8192, policy_version: 'topic-view-v1' };
        setTopicEdit({ view_id: item?.view_id ?? crypto.randomUUID(), revision: item?.revision ?? 0,
          definition: { ...definition, entries: definition.entries.join('\n'), sessions: JSON.stringify(definition.sessions),
            source_ids: definition.source_ids.join('\n'), observation_keys: definition.observation_keys.join('\n'),
            since: definition.since ?? '', until: definition.until ?? '' } });
      }
      function saveTopic() { return perform(async signal => {
        let definition;
        try {
          const value = topicEdit.definition;
          const lines = input => input.split('\n').map(item => item.trim()).filter(Boolean);
          const sessions = JSON.parse(value.sessions);
          if (!Array.isArray(sessions) || sessions.some(item => !Array.isArray(item) || item.length !== 2 || item.some(part => typeof part !== 'string'))) throw new Error();
          definition = { ...value, entries: lines(value.entries), sessions, source_ids: lines(value.source_ids), observation_keys: lines(value.observation_keys),
            since: value.since.trim() || null, until: value.until.trim() || null };
          for (const key of ['ttl_seconds', 'refresh_interval_seconds', 'max_sources', 'max_chars']) {
            if (!/^\d+$/.test(String(value[key])) || !Number.isSafeInteger(Number(value[key]))) throw new Error();
            definition[key] = Number(value[key]);
          }
        } catch { throw new Error('invalid_topic_form'); }
        try { await call({ action: 'topic_define', request_id: crypto.randomUUID(), view_id: topicEdit.view_id,
          expected_revision: topicEdit.revision, definition }, signal); }
        catch (cause) { if (alive.current && !signal.aborted) { refresh(); setNotice(t('topicUnknown')); } throw cause; }
        if (alive.current && !signal.aborted) { refresh(); setNotice(t('saved')); }
      }); }
      function confirmTopicControl() { return perform(async signal => {
        try { await call({ action: 'topic_control', request_id: crypto.randomUUID(), view_id: topicControl.view_id,
          expected_revision: topicControl.revision, control: topicControl.control }, signal); }
        catch (cause) { if (alive.current && !signal.aborted) { refresh(); setNotice(t('topicUnknown')); } throw cause; }
        if (alive.current && !signal.aborted) { refresh(); setNotice(t('saved')); }
      }); }
      function preview(operation) { return perform(async signal => {
        const epoch = readEpoch.current;
        const result = await call({ action: 'preview', operation }, signal);
        if (alive.current && !signal.aborted && epoch === readEpoch.current) { setReview(result); setEditing(null); setSource(null); }
      }); }
      function cancelReview() { return perform(async signal => {
        await call({ action: 'cancel', proposal_id: review.proposal_id }, signal);
        if (alive.current) setReview(null);
      }); }
      function openSource(messageId, cursor = 0) { return perform(async signal => {
        const epoch = readEpoch.current;
        let result;
        try { result = await versionedRead({ view: 'source', message_id: messageId, cursor }, signal, cursor ? null : 'source'); }
        catch (error) { if (cursor && alive.current && epoch === readEpoch.current) expire(); throw error; }
        if (cursor && (!source || source.message_id !== messageId || source.state_version !== result.state_version || source.content_version !== result.content_version)) { expire(); throw new Error('source_unavailable'); }
        if (alive.current && !signal.aborted && epoch === readEpoch.current) setSource(previous => cursor && previous?.message_id === messageId ? { ...result, text: previous.text + result.text } : result);
      }); }
      function openHistory(tagId) { return perform(async signal => {
        const epoch = readEpoch.current;
        const result = await versionedRead({ view: 'history', tag_id: tagId, limit: 50 }, signal, 'history');
        if (alive.current && !signal.aborted && epoch === readEpoch.current) setHistory(result);
      }); }
      function openRecovery(id) { return perform(async signal => {
        const result = await call({ action: 'recovery_read', session_id: id }, signal);
        if (alive.current) setRecovery(result);
      }); }
      function closeRecovery() { return perform(async signal => {
        if (recovery.proposal_id && !recovery.result) await call({ action: 'recovery_cancel', proposal_id: recovery.proposal_id }, signal);
        if (alive.current) setRecovery(null);
      }); }
      const changeTab = name => { invalidateReads(); setData({ items: [] }); setLoading(true); setTab(name); setCategory(''); setNetworkCategoryInput(''); setFilter(''); setQuery(''); setTimelineInput({ entry: '', session: '', since: '', until: '' }); setTimelineFilter({ entry: '', session: '', since: '', until: '' }); setOffsets([0]); setNotice(''); };
      const sourceButtons = record => h('div', { className: 'en-actions' }, ...record.draft.sources.map((item, index) =>
        button(`${t('source')} ${index + 1}`, () => openSource(item.message_id), { key: item.message_id + ':' + index, disabled: busy })));
      const profileCard = record => h('article', { className: 'en-card', key: record.tag_id },
        h('header', null, h('span', { className: 'en-pill' }, t(record.draft.category)), h('span', { className: 'en-muted' }, `${t('version')} ${record.revision}`)),
        h('p', { className: 'en-label' }, record.draft.label),
        h('div', { className: 'en-meta' }, h('span', null, `${t('received')} · ${time(record.recorded_at)}`), h('span', null, t(record.state))),
        sourceButtons(record),
        h('div', { className: 'en-actions' }, button(t('history'), () => openHistory(record.tag_id), { disabled: busy }),
          record.state === 'confirmed' || record.state === 'draft' ? button(t('revise'), () => setEditing({ kind: 'profile_revise', tag_id: record.tag_id, label: record.draft.label }), { disabled: busy }) : null,
          record.state === 'confirmed' ? button(t('remove'), () => preview({ kind: 'profile_suppress', tag_id: record.tag_id }), { disabled: busy }) : null,
          record.state === 'draft' ? h(React.Fragment, null,
            button(t('accept'), () => preview({ kind: 'profile_confirm', tag_id: record.tag_id }), { className: 'en-primary', disabled: busy }),
            button(t('reject'), () => preview({ kind: 'profile_reject', tag_id: record.tag_id }), { disabled: busy })) : null,
          record.state === 'rejected' ? button(t('reopen'), () => preview({ kind: 'profile_reopen', tag_id: record.tag_id }), { disabled: busy }) : null));
      const networkCard = record => networkLayer !== 'profiles' ? h('article', { className: 'en-network-cluster', key: record.fact_id ?? record.observation_id },
        h('div', { className: 'en-network-node' }, h('span', { className: 'en-pill' }, t(networkLayer)),
          h('span', { className: 'en-muted' }, ' · ' + t('version') + ' ' + record.revision + ' · '),
          h('span', null, t(record.state)), record.inference ? h('span', { className: 'en-pill' }, t('inference')) : null,
          h('p', null, record.text ?? record.conclusion)),
        h('div', { className: 'en-network-edges', 'aria-label': t('sources') },
          ...(record.sources ?? record.support ?? []).map((id, index) => button(t(record.sources ? 'networkSource' : 'support') + ' ' + (index + 1),
            () => openSource(id), { key: 'support:' + id + ':' + index, disabled: busy })),
          ...(record.counterevidence ?? []).map((id, index) => button(t('counterevidence') + ' ' + (index + 1),
            () => openSource(id), { key: 'counter:' + id + ':' + index, disabled: busy })))) : h('article', { className: 'en-network-cluster', key: record.tag_id },
        h('div', { className: 'en-network-node' },
          h('span', { className: 'en-pill' }, t(record.draft.category)),
          h('span', { className: 'en-muted' }, ' · ' + t('version') + ' ' + record.revision),
          h('p', null, record.draft.label)),
        h('div', { className: 'en-network-edges', 'aria-label': t('sources') },
          ...record.draft.sources.map((item, index) => button(t('networkSource') + ' ' + (index + 1),
            () => openSource(item.message_id), { key: item.message_id + ':' + index, disabled: busy }))));
      const graphCards = records => {
        const sources = new Map();
        for (const record of records) for (const edge of record.edges) {
          if (!sources.has(edge.source_id)) sources.set(edge.source_id, []);
          sources.get(edge.source_id).push({ record, stance: edge.kind });
        }
        return [...sources].map(([id, linked], index) => h('article', { className: 'en-network-cluster', key: id },
          button(t('networkSource') + ' ' + (index + 1), () => openSource(id), { disabled: busy }),
          h('div', { className: 'en-network-edges', 'aria-label': t('sources') }, ...linked.map(({ record, stance }, n) =>
            h('div', { className: 'en-network-node', key: record.kind + ':' + record.id + ':' + n },
              h('span', { className: 'en-pill' }, t(record.kind)),
              h('span', { className: 'en-muted' }, ' · ' + t('version') + ' ' + record.revision + ' · ' + t(record.state) + ' · ' + t(stance === 'evidence' ? 'graphEvidence' : stance)),
              record.inference ? h('span', { className: 'en-pill' }, t('inference')) : null,
              h('p', null, record.label))))));
      };
      const timelineCard = item => h('article', { className: 'en-card', key: item.message_id },
        h('header', null, h('span', { className: 'en-pill' }, t(item.role)), h('span', { className: 'en-muted' }, item.source.entry)),
        h('p', { className: 'en-label' }, item.excerpt),
        h('div', { className: 'en-meta' }, h('span', null, `${t('occurred')} · ${time(item.occurred_at)}`), h('span', null, `${t('received')} · ${time(item.received_at)}`)),
        h('div', { className: 'en-actions' }, button(t('source'), () => openSource(item.message_id), { disabled: busy }),
          recoverySupported && item.source.entry === currentEntry ? button(t('recovery'), () => openRecovery(item.source.session), { disabled: busy }) : null));
      if (!enabled) return null;
      const budgetMode = sessionStatus?.context_mode ?? contextMode;
      const personaEditable = !error && diagnostic?.available === true && Array.isArray(diagnostic.actions) && diagnostic.actions.includes('persona_set');
      return h('section', { className: 'en-mem' + (host === 'sidebar' ? ' en-side' : ''), 'aria-label': t('title') },
        h('style', null, css), h('div', { className: 'en-wrap' },
          h('div', { className: 'en-head' }, h('div', { className: 'en-brand' }, h('span', { className: 'en-mark', 'aria-hidden': true }, '❧'),
            h('div', null, h('h1', null, host === 'sidebar' ? t('session') : t('title')), h('div', { className: 'en-muted' }, t('subtitle')))),
            h('span', { className: 'en-pill' }, t('preview'))),
          h('div', { className: 'en-notice' }, t('notice')), h('p', { className: 'en-muted' }, t('leaseNotice')),
          privateCapture ? h('div', { className: 'en-actions' }, button(t('privateOpen'), () => perform(async signal => {
            await call({ action: 'private_open' }, signal); if (alive.current) { setPrivateResult(null); setNotice(t('privateRequested')); }
          }), { disabled: busy }), button(t('privateCheck'), () => perform(async signal => {
            const result = await call({ action: 'private_status' }, signal);
            const allowed = ['not_started', 'requested', 'saved', 'canceled', 'outcome_unknown', 'launch_failed'];
            const id = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
            if (!result || !allowed.includes(result.state) || (['requested', 'saved', 'canceled', 'outcome_unknown'].includes(result.state)
                && (!id.test(result.session_id) || !id.test(result.event_id)))) throw new Error('management_unavailable');
            if (alive.current) setPrivateResult(result);
          }), { disabled: busy }), h('span', { className: 'en-muted' }, t('privateHelp'))) : null,
          privateCapture && privateResult ? h('div', { className: 'en-notice', role: 'status' },
            h('p', null, t(({ not_started: 'privateNever', requested: 'privateWaiting', saved: 'privateSaved', canceled: 'privateCanceled',
              outcome_unknown: 'privateUnknown', launch_failed: 'privateFailed' })[privateResult.state])),
            privateResult.state === 'outcome_unknown' ? h(React.Fragment, null,
              h('p', null, t('privateId') + ': ' + privateResult.session_id + ' / ' + privateResult.event_id),
              h('p', null, t('privateRestart')),
              button(t('privateRetry'), () => perform(async signal => {
                await call({ action: 'private_retry' }, signal); if (alive.current) { setPrivateResult(null); setNotice(t('privateRequested')); }
              }), { disabled: busy }), button(t('privateAbandon'), () => perform(async signal => {
                const result = await call({ action: 'private_abandon' }, signal);
                if (alive.current && result?.state === 'not_started') setPrivateResult(result);
              }), { disabled: busy })) : null) : null,
          configured ? h('div', { className: 'en-notice' }, t('budgetMode') + ': ' + t(budgetMode === 'target_tokenizer'
            ? 'budgetTarget' : budgetMode === 'legacy_character_cap' ? 'budgetLegacy' : 'budgetUnknown')) : null,
          h('details', { className: 'en-notice' }, h('summary', null, t('diagnostics')),
            h('p', null, t('diagnosticScope')),
            h('p', null, t('filterEntry') + ': ' + currentEntry),
            h('p', null, t('diagnosticStream') + ': ' + (streamMode ?? t('unknown'))),
            h('p', null, t('diagnosticService') + ': ' + t(!configured ? 'config' : !diagnostic ? 'diagnosticLoading' : diagnostic.available ? 'diagnosticOnline' : 'diagnosticUnavailable')),
            h('p', null, t('diagnosticSchema') + ': ' + (diagnostic?.schema ?? t('unknown'))),
            h('p', null, t('diagnosticRecovery') + ': ' + t(recoverySupported ? 'diagnosticEnabled' : 'diagnosticDisabled')),
            h('p', null, t('diagnosticPrivate') + ': ' + t(privateCapture === null ? 'diagnosticLoading' : privateCapture ? 'diagnosticConfigured' : 'diagnosticNotConfigured')),
            h('p', null, t('diagnosticFingerprint') + ': ' + t(fingerprintBridge === null ? 'diagnosticLoading' : fingerprintBridge ? 'diagnosticConfigured' : 'diagnosticNotConfigured')),
            ...[['diagnosticEmbedding', 'embedding'], ['diagnosticReflection', 'reflection'], ['diagnosticExtraction', 'extraction'], ['diagnosticSpeech', 'speech']].map(([label, key]) => {
              const state = diagnostic?.models?.[key];
              const mode = state === 'configured_local' ? 'providerLocal' : state === 'configured_remote' ? 'providerRemote'
                : state === 'not_configured' ? 'providerNone' : state === 'host_owned' ? 'providerHost' : 'providerUnknown';
              return h('p', { key }, t(label) + ': ' + t(mode));
            }),
            h('p', null, t('validationHelp')),
            ...[['diagnosticEmbeddingFormat', 'embedding'], ['diagnosticContextFormat', 'context']].map(([label, key]) => {
              const state = diagnostic?.validation?.[key];
              const value = state === 'valid' ? 'validationValid' : state === 'invalid' ? 'validationInvalid'
                : state === 'not_configured' ? 'validationNone' : 'validationUnknown';
              return h('p', { key }, t(label) + ': ' + t(value));
            }),
            h('p', null, t('notice')), h('p', null, t('encryption'))),
          h('div', { className: 'en-values' }, ...['local', 'evidence', 'control'].map(key => h('span', { key }, '✓ ' + t(key)))),
          host === 'sidebar' ? h('p', { className: 'en-muted' }, t('sessionHelp')) : null,
          sessionStatus?.recovery_read_only ? h('p', { className: 'en-notice' }, t('recoveryReadOnly')) : null,
          recoverySupported && sessionId ? h('div', { className: 'en-actions' }, button(t('recovery'), () => openRecovery(sessionId), { disabled: busy })) : null,
          sessionStatus ? h('details', { className: 'en-notice' }, h('summary', null, t('status')),
            h('p', null, t(sessionStatus.stream_mode === 'durable_text' ? 'textOnly'
              : sessionStatus.stream_mode === 'durable_tools' ? 'durableTools'
              : sessionStatus.tool_publication === 'saved_before_publish' ? 'bufferedTools' : 'buffered')),
            h('p', null, t('gaps') + ': ' + (sessionStatus.capture_gaps?.join(', ') || t('noGaps')))) : null,
          h('nav', { className: 'en-tabs', role: 'tablist', 'aria-label': t('title') }, ...['profiles', 'interests', 'persona', 'timeline', 'network', 'topic_settings', 'drafts', 'suppressed', 'rejected'].map(name =>
            button(t(name), () => changeTab(name), { key: name, role: 'tab', 'aria-selected': name === tab }))),
          notice ? h('div', { className: 'en-success', role: 'status' }, notice) : null,
          error ? h('div', { className: 'en-error', role: 'alert' }, error) : null,
          configured === false ? h('div', { className: 'en-empty' }, h('h2', null, t('config')), h('p', null, t('configHelp'))) :
          h(React.Fragment, null,
            tab === 'topic_settings' ? h('div', { className: 'en-toolbar' }, button(t('topicNew'), () => editTopic(null), { disabled: loading || busy }),
              button(t('refresh'), refresh, { disabled: loading || busy }), h('span', { className: 'en-muted' }, t('topicControlHelp'))) :
            h('form', { className: 'en-toolbar', onSubmit: event => { event.preventDefault(); refresh(); setFilter(query); if (tab === 'timeline' || tab === 'network') setTimelineFilter(timelineInput); if (tab === 'network') setCategory(networkCategoryInput); setOffsets([0]); } },
              tab === 'network' ? h('select', { value: networkLayer, 'aria-label': t('networkLayer'), onChange: event => { invalidateReads(); setData({ items: [] }); setLoading(true); setNetworkLayer(event.target.value); setQuery(''); setFilter(''); setCategory(''); setNetworkCategoryInput(''); setOffsets([0]); } },
                ...['graph', 'profiles', 'facts', 'observations'].map(name => h('option', { key: name, value: name }, t(name)))) : null,
              h('input', { value: query, maxLength: 256, placeholder: t('search'), 'aria-label': t('search'), onChange: event => setQuery(event.target.value) }),
              ...(tab === 'timeline' || tab === 'network' ? ['entry', ...(host === 'sidebar' && tab === 'timeline' ? [] : ['session']), 'since', 'until'].map(key => {
                const label = 'filter' + key[0].toUpperCase() + key.slice(1);
                return h('label', { key }, t(label), h('input', { type: key === 'since' || key === 'until' ? 'date' : 'text',
                  maxLength: key === 'entry' || key === 'session' ? 256 : undefined, value: timelineInput[key], 'aria-label': t(label),
                  onChange: event => setTimelineInput(value => ({ ...value, [key]: event.target.value })) }));
              }) : []),
              tab === 'network' ? h('label', null, t('networkCategory'), h('input', { value: networkCategoryInput, maxLength: 256,
                'aria-label': t('networkCategory'), onChange: event => setNetworkCategoryInput(event.target.value) })) : null,
              tab !== 'timeline' && tab !== 'interests' && tab !== 'persona' && tab !== 'network' ? h('select', { value: category, 'aria-label': t('all'), onChange: event => { refresh(); setCategory(event.target.value); setOffsets([0]); } },
                ...['', 'identity', 'preference', 'interest', 'relation', 'goal', 'boundary'].map(name => h('option', { key: name, value: name }, t(name || 'all')))) : null,
              h('button', { type: 'submit', disabled: loading }, t('searchGo')), button(t('refresh'), refresh, { disabled: loading || busy })),
            tab === 'network' ? h(React.Fragment, null, h('p', { className: 'en-muted' }, t('networkHelp')),
              h('p', { className: 'en-muted' }, t('networkCategoryHelp'))) : null,
            tab === 'persona' ? h('div', { className: 'en-actions' }, h('p', { className: 'en-muted' }, t('personaIndependent')),
              button(t('personaNew'), () => setEditing({ kind: 'persona_set', block: 'identity', text: '' }), { disabled: busy || loading || !personaEditable }),
              !personaEditable ? h('p', { className: 'en-muted' }, t('personaUnsupported')) : null) : null,
            tab === 'timeline' || tab === 'network' ? h('p', { className: 'en-muted' }, t('filterTimeHelp')) : null,
            loading ? h('p', { role: 'status' }, t('loading')) : data.items.length ?
              h('div', { className: tab === 'network' ? 'en-network' : tab === 'timeline' ? 'en-rows' : 'en-grid' },
                ...(tab === 'persona' ? data.items.map(item => h('article', { key: item.block, className: 'en-card' },
                  h('header', null, h('span', { className: 'en-pill' }, t('persona_' + item.block)),
                    h('span', { className: 'en-muted' }, t('version') + ' ' + item.revision)),
                  h('p', { className: 'en-label' }, item.text),
                  item.evidence_id ? button(t('source'), () => openSource(item.evidence_id), { disabled: busy })
                    : h('p', { className: 'en-muted' }, t('personaSourceUnavailable')),
                  button(t('personaEdit'), () => setEditing({ kind: 'persona_set', block: item.block, text: item.text }), { disabled: busy || !personaEditable }))) :
                tab === 'topic_settings' ? data.items.map(item => h('article', { key: item.view_id, className: 'en-card' },
                  h('h2', null, item.definition?.title ?? item.view_id), h('p', null, item.definition?.purpose),
                  h('p', { className: 'en-muted' }, t('version') + ' ' + item.revision + ' · ' + t(['valid', 'conflict'].includes(item.state) ? item.state : 'topic_' + item.state)),
                  h('div', { className: 'en-actions' }, button(t('topicEdit'), () => editTopic(item), { disabled: busy }),
                    button(t(item.state === 'paused' ? 'topicResume' : 'topicPause'), () => setTopicControl({ ...item, control: item.state === 'paused' ? 'resume' : 'pause' }), { disabled: busy }),
                    button(t('topicDelete'), () => setTopicControl({ ...item, control: 'delete' }), { disabled: busy })))) :
                  tab === 'network' && networkLayer === 'graph' ? graphCards(data.items) : data.items.map(tab === 'network' ? networkCard : tab === 'timeline' ? timelineCard : profileCard))) :
              error ? null : h('div', { className: 'en-empty' }, h('h2', null, t(tab === 'persona' ? 'personaEmpty' : tab === 'topic_settings' ? 'topicEmpty' : 'empty')), h('p', null, t(tab === 'persona' ? 'personaIndependent' : tab === 'topic_settings' ? 'topicEmptyHelp' : tab === 'network' ? 'networkEmpty' : 'emptyHelp'))),
            error && !data.items.length ? null : h('div', { className: 'en-page' }, h('span', { className: 'en-muted' }, `${t('page')} ${offsets.length} · ${t('currentPage')} ${data.items.length}`),
              button(t('previous'), () => { refresh(); setOffsets(value => value.slice(0, -1)); }, { disabled: loading || offsets.length < 2 }),
              button(t('next'), () => { const next = data.next_offset; refresh(); setOffsets(value => [...value, next]); }, { disabled: loading || data.next_offset == null }))),
          h('p', { className: 'en-muted' }, t('encryption')),
          topicEdit ? h(Modal, { title: t(topicEdit.revision ? 'topicEdit' : 'topicNew'), close: () => setTopicEdit(null), busy, error },
            h('p', { className: 'en-muted' }, t('topicControlHelp')),
            ...[['title', 'topicTitle'], ['purpose', 'topicPurpose'], ['query', 'topicQuery'], ['entries', 'topicEntries'],
              ['sessions', 'topicSessions'], ['source_ids', 'topicSources'], ['observation_keys', 'topicObservations'],
              ['since', 'topicSince'], ['until', 'topicUntil'], ['ttl_seconds', 'topicTTL'], ['refresh_interval_seconds', 'topicInterval'],
              ['max_sources', 'topicMaxSources'], ['max_chars', 'topicMaxChars'], ['policy_version', 'topicPolicy']].map(([key, label]) =>
                h('label', { key, className: 'en-topic-field' }, t(label), h(['entries', 'sessions', 'source_ids', 'observation_keys'].includes(key) ? 'textarea' : 'input',
                  { value: topicEdit.definition[key], 'aria-label': t(label), onChange: event => setTopicEdit(previous => ({ ...previous,
                    definition: { ...previous.definition, [key]: event.target.value } })) }))),
            h('div', { className: 'en-footer' }, button(t('cancel'), () => setTopicEdit(null), { disabled: busy }),
              button(t('topicConfirm'), saveTopic, { className: 'en-primary', disabled: busy }))) : null,
          topicControl ? h(Modal, { title: t('topicConfirm'), close: () => setTopicControl(null), busy, error },
            h('p', null, t(({ pause: 'topicPause', resume: 'topicResume', delete: 'topicDelete' })[topicControl.control]) + ' · ' + topicControl.definition.title),
            topicControl.control === 'delete' ? h('p', null, t('topicDeleteHelp')) : null,
            h('p', { className: 'en-muted' }, t('topicControlHelp')),
            h('div', { className: 'en-footer' }, button(t('cancel'), () => setTopicControl(null), { disabled: busy }),
              button(t('topicConfirm'), confirmTopicControl, { className: 'en-primary', disabled: busy }))) : null,
          editing ? h(Modal, { title: t(editing.kind === 'persona_set' ? 'personaEdit' : 'revise'), close: () => setEditing(null), busy, error },
            h('p', { className: 'en-muted' }, t(editing.kind === 'persona_set' ? 'personaSecret' : 'reviseHelp')),
            editing.kind === 'persona_set' ? h('label', null, t('personaBlock'), h('select', { value: editing.block, 'aria-label': t('personaBlock'),
              onChange: event => setEditing(value => ({ ...value, block: event.target.value, text: '' })) },
              ...['identity', 'tone', 'boundary', 'address', 'state'].map(block => h('option', { key: block, value: block }, t('persona_' + block))))) : null,
            h('textarea', { value: editing.label ?? editing.text, maxLength: editing.kind === 'profile_revise' ? 256 : 16384, 'aria-label': t('replacement'),
              onChange: event => setEditing(value => ({ ...value, [value.kind === 'profile_revise' ? 'label' : 'text']: event.target.value })) }),
            h('div', { className: 'en-footer' }, button(t('cancel'), () => setEditing(null), { disabled: busy }),
              editing.kind === 'persona_set' ? button(t('personaClear'), () => preview({ ...editing, text: '' }), { disabled: busy }) : null,
              button(t('review'), () => preview(editing), { className: 'en-primary', disabled: busy || !(editing.label ?? editing.text).trim() }))) : null,
          review ? h(Modal, { title: t('review') + ' · ' + t(({ profile_confirm: 'accept', profile_reject: 'reject', profile_suppress: 'remove',
              profile_revise: 'revise', source_correct: 'revise', source_suppress: 'suppress', profile_reopen: 'reopen', persona_set: 'personaEdit' })[review.operation.kind]), close: cancelReview, busy, error },
            h('p', null, t(review.operation.kind === 'persona_set' ? 'personaSecret' : review.operation.kind === 'profile_suppress' ? 'removeHelp' : review.operation.kind === 'source_suppress' ? 'suppressHelp' : review.operation.kind === 'profile_reject' ? 'rejectHelp' : 'originalRetained')),
            review.operation.kind === 'persona_set' ? h('p', null, t('personaBlock') + ': ' + t('persona_' + review.operation.block)) : null,
            review.previous ? h('div', null, h('h2', null, t('old')), h('pre', null, review.operation.kind === 'persona_set' ? review.previous.text : review.previous.draft.label)) : null,
            review.operation.kind === 'persona_set' && review.operation.text === '' ? h('p', null, t('personaClearValue')) : null,
            review.operation.label || review.operation.text ? h('div', null, h('h2', null, t('replacement')), h('pre', null, review.operation.label ?? review.operation.text)) : null,
            h('h2', null, t('sources')), ...review.sources.map(item => h('div', { key: item.message_id }, h('p', { className: 'en-muted' }, `${t('version')} ${item.state_version} · ${t('received')} ${time(item.received_at)}`), h('pre', null, item.excerpt),
              item.total > Array.from(item.excerpt).length ? h('p', { className: 'en-muted' }, t('partial')) : null,
              button(t('source'), () => openSource(item.message_id), { disabled: busy }))),
            review.affected ? h('div', null, h('h2', null, t('affected')), review.affected.length ?
              h('ul', null, ...review.affected.map((item, index) => h('li', { key: index }, `${item.kind} · ${item.label ?? item.id}${item.revision ? ' · ' + t('version') + ' ' + item.revision : ''}`))) : h('p', { className: 'en-muted' }, t('noDerived'))) : null,
            h('p', { className: 'en-muted' }, t('originalRetained')),
            h('div', { className: 'en-footer' }, button(t('cancel'), cancelReview, { disabled: busy }),
              button(t('confirm'), () => perform(async signal => {
                let outcome;
                try { outcome = await call({ action: 'confirm', proposal_id: review.proposal_id, operation: review.operation }, signal); }
                catch (cause) { if (review.operation.kind === 'persona_set' && alive.current && !signal.aborted) { refresh(); setNotice(t('personaUnknown')); } throw cause; }
                if (alive.current) { setReview(null); setNotice(t('saved') + ' · ' + t('version') + ' ' + outcome.revision); updates.dispatchEvent(new Event('changed')); }
              }), { className: 'en-primary', disabled: busy }))) : null,
          recovery ? h(Modal, { title: t('recovery'), close: closeRecovery, busy, error },
            h('p', null, t('recoveryHelp')), h('p', { className: 'en-notice' }, t('recoveryUnknown')),
            !recovery.blocked ? h('p', null, t(recovery.read_only ? 'recoveryReadOnly' : 'recoveryNone')) :
            h('div', { className: 'en-rows' }, ...recovery.calls.map(item => h('article', { className: 'en-card', key: item.key },
              h('h2', null, item.name), h('p', null, t(item.result === 'complete' ? 'recoveryComplete' : item.result === 'partial' ? 'recoveryPartial' : 'recoveryUnconfirmed'))))),
            ...recovery.evidence.map(item => h('article', { className: 'en-card', key: item.message_id },
              h('p', { className: 'en-muted' }, t(({ input: 'recoveryInput', call: 'recoveryCall', result: 'recoveryResult', 'ptc-call': 'recoveryPtcCall', 'ptc-log': 'recoveryPtcLog' })[item.phase])),
              h('pre', null, item.excerpt), button(t('source'), () => openSource(item.message_id), { disabled: busy }))),
            recovery.result ? h('div', { role: 'status' }, h('p', null, t('recoveryCreated')), h('p', { className: 'en-muted' }, recovery.result.target_session),
              button(t('recoveryOpen'), () => perform(async () => {
                await ctx.get('sessions').refresh(); ctx.get('uiWorkspace').openSession(recovery.result.target_session); setRecovery(null);
              }), { disabled: busy || !ctx.get?.('uiWorkspace') })) :
            recovery.blocked && !recovery.read_only ? h('div', { className: 'en-footer' },
              button(t('cancel'), closeRecovery, { disabled: busy }),
              button(t(recovery.proposal_id ? 'recoveryConfirm' : 'recoveryPreview'), () => perform(async signal => {
                if (!recovery.proposal_id) {
                  const preview = await call({ action: 'recovery_preview', session_id: recovery.session_id }, signal);
                  if (alive.current) setRecovery(preview);
                } else {
                  const result = await call({ action: 'recovery_confirm', session_id: recovery.session_id, proposal_id: recovery.proposal_id, fingerprint: recovery.fingerprint }, signal);
                  if (alive.current) setRecovery({ ...recovery, result });
                }
              }), { className: 'en-primary', disabled: busy })) : null) : null,
          source ? h(Modal, { title: t(source.representation === 'original' ? 'raw' : 'projection'), close: () => { dropLease('source'); setSource(null); }, busy, error },
            h('div', { className: 'en-meta' }, `${source.source.entry} · ${source.source.session}`, h('span', null, `${t('received')} · ${time(source.received_at)}`)),
            h('pre', null, source.text), source.next_cursor !== null ? button(t('more'), () => openSource(source.message_id, source.next_cursor), { disabled: busy }) : null,
            source.representation === 'original' ? h('div', { className: 'en-actions' },
              button(t('revise'), () => { setEditing({ kind: 'source_correct', message_id: source.message_id, text: '' }); setSource(null); setReview(null); }, { disabled: busy }),
              button(t('suppress'), () => preview({ kind: 'source_suppress', message_id: source.message_id }), { disabled: busy })) : null) : null,
          history ? h(Modal, { title: t('history'), close: () => { dropLease('history'); setHistory(null); }, busy, error }, ...history.items.map(item => h('article', { key: item.revision, className: 'en-card' },
            h('div', { className: 'en-meta' }, `${t('version')} ${item.revision} · ${t(item.state)} · ${time(item.recorded_at)}`), h('pre', null, item.draft.label))),
            history.next_offset !== null ? h('p', { className: 'en-muted' }, t('partial')) : null) : null));
    }
    const page = props => h(MemoryPage, props);
    const icon = () => h('span', { 'aria-hidden': true }, '❧');
    own(ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'dsh-evernook', locale: ns }, page)));
    own(ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: 'dsh-evernook', order: 25, label: () => t('title'), locale: ns }, icon)));
    own(ctx.inject(['sidebarRightTabs'], c => {
      c.effect(() => c.sidebarRightTabs.register({ id: 'dsh-evernook', kind: 'dsh-evernook', title: () => t('session'),
        guide: [{ id: 'dsh-evernook', order: 25, title: () => t('session'), description: () => t('subtitle'), icon }] }));
      own(c.slots.inject('sidebar.right.pane.tab', () => c.slots.register({ name: 'sidebar.right.pane.tab', key: 'dsh-evernook', locale: ns }, props => page({ ...props, host: 'sidebar' }))));
      own(c.slots.inject('sidebar.right.pane.tab.title', () => c.slots.register({ name: 'sidebar.right.pane.tab.title', key: 'dsh-evernook' }, () => t('session'))));
    }));
    ctx.effect(() => async () => {
      active = false; bootstrap = undefined;
      updates.dispatchEvent(new Event('disposed'));
      for (const dispose of resources.reverse()) await dispose();
    });
  }
  return { name: 'dsh-evernook-client', inject: ['slots', 'locale', 'sessions', 'uiWorkspace'], apply };
} });
