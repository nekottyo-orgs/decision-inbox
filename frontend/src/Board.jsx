import {useEffect, useRef, useState} from 'react';
import {dateText} from './DecisionDesk.jsx';

// Board (所属記録の観測と依頼 thread)。見せ方は Vercel Board の inspector に寄せ、
// データは Cue の DB (/api/board と /api/tasks/{id}/thread) をそのまま表示する。
const LANE_LABELS = {todo: '未着手', human_decision: '人の判断待ち', worker: 'worker 作業中', human_action: '人の操作待ち', done: '完了'};
const KIND_LABELS = {decision: '判断', monitor: '監視', research: '調査', implement: '実装'};
const CLOSE_LABELS = {normal: '通常終了', replaced: '差し替え', crashed: '異常終了', closed: '閉鎖'};
const CLASS_LABELS = {expected: '想定内', unexpected: '想定外'};

const timeText = value => new Intl.DateTimeFormat('ja-JP', {hour: '2-digit', minute: '2-digit'}).format(new Date(value));
const shortId = value => value && value.length > 12 ? `${value.slice(0, 8)}…` : value;
const allTasks = lanes => lanes.flatMap(lane => lane.tasks);

async function fetchJson(path, body) {
  const response = await fetch(path, body === undefined ? {cache: 'no-store'} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'サーバーに接続できませんでした');
  return data;
}

function Labels({task}) {
  return <span className="board-labels">
    {task.kind && <span className="board-lab">kind-{task.kind}</span>}
    <span className="board-lab">lane-{task.lane}</span>
  </span>;
}

function FeedRow({head, children}) {
  return <div className="board-frow">
    <span className="board-avatar">{head.initial}</span>
    <div className="board-fbody">
      <div className="board-fhead">
        <span className="board-actor">{head.actor} <span className="board-via">via {head.via}</span></span>
        <span>{head.action}</span>
        {head.chan && <span className="board-chan">#{head.chan}</span>}
        <span className="board-ago">{head.when}</span>
      </div>
      {children}
    </div>
  </div>;
}

// 全依頼横断の最新フィード。投稿 (配送試行) と窓口が受けた msg と、人待ちの blocked 行を混ぜる
function Activity({board, lanes, decisions, onOpenThread, onOpenDecision}) {
  const tasks = allTasks(lanes);
  const byId = new Map(tasks.map(task => [task.id, task]));
  const rows = [];
  for (const post of board.posts) {
    const task = byId.get(post.task_id);
    rows.push({at: post.created_at, key: `post-${post.id}`, node: <FeedRow head={{
      initial: '人', actor: '人', via: post.target_session ? shortId(post.target_session) : '宛先なし',
      action: 'posted in', chan: post.task_id, when: timeText(post.created_at)}}>
      <div className="board-fline"><span className={`board-pfx ${post.state === 'received' ? 'done' : ''}`}>[{post.state === 'received' ? '受領済み' : '未受領'}]</span> {post.body}</div>
      <div className="board-fline board-sub">分類: {CLASS_LABELS[post.classification]} · 宛先: {post.target_session || '担当不在 (未受領のまま残ります)'}</div>
      {task && <Labels task={task} />}
    </FeedRow>});
  }
  for (const task of tasks) {
    if (!['human_decision', 'human_action'].includes(task.lane)) continue;
    const decision = task.decision_id ? decisions.find(item => item.id === task.decision_id) : null;
    const waiting = task.lane === 'human_decision'
      ? `awaiting_approval: ${decision ? decision.title : '判断待ち'}`
      : 'awaiting_action: 人の操作待ち';
    rows.push({at: task.updated_at, key: `blocked-${task.id}`, node: <FeedRow head={{
      initial: '!', actor: task.id, via: task.run || '—', action: 'blocked in', chan: task.id, when: timeText(task.updated_at)}}>
      <div className="board-blocked"><span>blocked · {waiting}</span>
        {decision && !decision.answer && <button type="button" className="board-btn small" onClick={() => onOpenDecision(decision.id)}>判断キューで回答</button>}
        <button type="button" className="board-btn small" onClick={() => onOpenThread(task.id)}>thread を開く</button>
      </div>
      <Labels task={task} />
    </FeedRow>});
  }
  rows.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  if (!rows.length) return <p className="board-empty">まだ動きはありません。</p>;
  return <div className="board-feed">{rows.slice(0, 40).map(row => <div key={row.key}>{row.node}</div>)}</div>;
}

// 所属マップ。開いている run を状態別の列に置き、run を開くと所属のタイムラインを出す
function RunMap({board, lanes, selectedRun, onSelectRun}) {
  const tasks = allTasks(lanes);
  const byRun = new Map();
  for (const session of board.sessions) {
    if (!byRun.has(session.run_id)) byRun.set(session.run_id, []);
    byRun.get(session.run_id).push(session);
  }
  for (const run of board.runs) if (!byRun.has(run.run_id)) byRun.set(run.run_id, []);
  const taskByRun = new Map();
  for (const task of tasks) {
    if (!task.run) continue;
    if (!taskByRun.has(task.run)) taskByRun.set(task.run, []);
    taskByRun.get(task.run).push(task);
  }
  const columnOf = (runId, sessions) => {
    const linked = taskByRun.get(runId) || [];
    if (linked.some(task => ['human_decision', 'human_action'].includes(task.lane))) return 'waiting';
    if (sessions.some(s => s.closed_at && s.close_reason === 'replaced') && sessions.some(s => !s.closed_at)) return 'replaced';
    if (sessions.some(s => !s.closed_at && ['worker', 'cue_worker'].includes(s.role))) return 'working';
    return 'idle';
  };
  const columns = [
    {id: 'working', label: '稼働中', cards: []},
    {id: 'waiting', label: '人待ち', cards: []},
    {id: 'replaced', label: '差し替え中', cards: []},
    {id: 'idle', label: '待機・未割当', cards: []},
  ];
  for (const [runId, sessions] of byRun) {
    const open = sessions.filter(s => !s.closed_at);
    if (!sessions.length && !board.runs.find(run => run.run_id === runId && !run.closed_at)) continue;
    columns.find(column => column.id === columnOf(runId, sessions)).cards.push({runId, sessions, open});
  }
  for (const task of tasks) {
    const sessions = task.run ? byRun.get(task.run) || [] : [];
    if (!sessions.some(s => !s.closed_at)) {
      columns.find(column => column.id === 'idle').cards.push({task});
    }
  }
  const selected = selectedRun ? byRun.get(selectedRun) || [] : null;
  return <div>
    <div className="board-kanban">
      {columns.map(column => <section key={column.id} className="board-col" aria-label={column.label}>
        <h3>{column.label}<span className="board-count">{column.cards.length}</span></h3>
        {column.cards.map(card => card.task
          ? <button type="button" key={`task-${card.task.id}`} className="board-card" onClick={() => onSelectRun(null)}>
              <span className="board-card-title">{card.task.title}</span>
              <span className="board-card-meta">#{card.task.id} · 担当なし</span>
              <Labels task={card.task} />
            </button>
          : <button type="button" key={card.runId} className="board-card" aria-current={selectedRun === card.runId ? 'true' : undefined}
              onClick={() => onSelectRun(selectedRun === card.runId ? null : card.runId)}>
              <span className="board-card-title mono">{card.runId}</span>
              {card.open.map(session => <span key={session.id} className="board-card-meta">
                {session.role} · {session.cli} · <span className="mono">{shortId(session.native_session_id)}</span> · {session.pane}
              </span>)}
              {!card.open.length && <span className="board-card-meta">開いている session なし</span>}
              {(taskByRun.get(card.runId) || []).map(task => <Labels key={task.id} task={task} />)}
            </button>)}
        {!column.cards.length && <p className="board-empty">なし</p>}
      </section>)}
    </div>
    {selectedRun && <section className="board-timeline" aria-label="所属タイムライン">
      <h3>所属タイムライン · <span className="mono">{selectedRun}</span></h3>
      <ol>
        {[...(selected || [])].sort((a, b) => Date.parse(a.opened_at) - Date.parse(b.opened_at)).map(session => <li key={session.id}>
          <span className="mono">{dateText(session.opened_at)}</span>
          <span>{session.role} ({session.cli})</span>
          <span className="mono">{shortId(session.native_session_id)}</span>
          <span>{session.pane} · {session.socket}</span>
          {session.req_id && <span>req: {session.req_id}</span>}
          {session.closed_at
            ? <span className="board-closed">→ {dateText(session.closed_at)} 終了 ({CLOSE_LABELS[session.close_reason] || session.close_reason})</span>
            : <span className="board-open">開いています</span>}
        </li>)}
      </ol>
      {!(selected || []).length && <p className="board-empty">この run の session 記録はありません。</p>}
    </section>}
  </div>;
}

// 依頼 thread。msg (メタデータのみ)・所属の区切り・投稿 (配送試行) を 1 本の時系列に混ぜる
function Thread({taskId, lanes, onOpenDecision}) {
  const [thread, setThread] = useState(null);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [postError, setPostError] = useState('');
  const alive = useRef(true);

  async function load() {
    try {
      const data = await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/thread`);
      if (alive.current) { setThread(data); setError(''); }
    } catch {
      if (alive.current) setError('thread を読み込めませんでした。自動で再試行します。');
    }
  }
  useEffect(() => {
    alive.current = true;
    setThread(null);
    setDraft('');
    setPostError('');
    load();
    const timer = setInterval(load, 5000);
    return () => { alive.current = false; clearInterval(timer); };
  }, [taskId]);

  async function send() {
    const body = draft.trim();
    if (!body || sending) return;
    setSending(true);
    setPostError('');
    try {
      await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/posts`, {body});
      setDraft('');
      await load();
    } catch (sendError) {
      setPostError(`投稿できませんでした。${sendError.message}。入力は保持しています。`);
    } finally { setSending(false); }
  }

  if (error && !thread) return <p className="board-empty" role="alert">{error}</p>;
  if (!thread) return <p className="board-empty">thread を読み込んでいます。</p>;
  const task = thread.task;
  const items = [];
  for (const session of thread.sessions) {
    items.push({at: session.opened_at, key: `open-${session.id}`, marker: `${session.run_id} · ${session.role} session 開始 · ${timeText(session.opened_at)}`});
    if (session.closed_at) items.push({at: session.closed_at, key: `close-${session.id}`, marker: `${session.run_id} · ${session.role} session 終了 (${CLOSE_LABELS[session.close_reason] || session.close_reason}) · ${timeText(session.closed_at)}`});
  }
  for (const message of thread.messages) {
    items.push({at: message.received_at, key: `msg-${message.id}`, node: <FeedRow head={{
      initial: '窓', actor: message.from_pane, via: message.run_id, action: 'received', when: timeText(message.received_at)}}>
      <div className="board-fline"><span className="board-pfx">[{message.state}]</span> <span className="mono">{message.msg}</span></div>
      {message.wait && <div className="board-fline board-sub">待ち: {message.wait}</div>}
    </FeedRow>});
  }
  for (const post of thread.posts) {
    items.push({at: post.created_at, key: `post-${post.id}`, node: <FeedRow head={{
      initial: '人', actor: '人', via: post.target_session ? shortId(post.target_session) : '宛先なし', action: 'posted', when: timeText(post.created_at)}}>
      <div className="board-fline body">{post.body}</div>
      <div className="board-fline board-sub">
        <span className={`board-pfx ${post.state === 'received' ? 'done' : ''}`}>[{post.state === 'received' ? `受領済み ${timeText(post.received_at)}` : '未受領'}]</span>
        {' '}分類: {CLASS_LABELS[post.classification]} · 宛先: {post.target_session ? `${post.target_session} (${post.target_pane})` : '担当不在'}
      </div>
    </FeedRow>});
  }
  items.sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  return <div className="board-thread">
    <header className="board-thread-head">
      <h3>#{task.id} {task.title}</h3>
      <div className="board-thread-meta">
        <span className="board-lab">{LANE_LABELS[task.lane] || task.lane}</span>
        {task.kind && <span className="board-lab">{KIND_LABELS[task.kind] || task.kind}</span>}
        {task.run && <span className="board-lab mono">{task.run}</span>}
        {task.decision && <span className="board-lab">判断: {task.decision.answered ? '回答済み' : '未回答'} · {task.decision.title}</span>}
        {task.decision_id && task.decision && !task.decision.withdrawn &&
          <button type="button" className="board-btn small" onClick={() => onOpenDecision(task.decision_id)}>判断を開く</button>}
      </div>
      <p className="board-note">msg の本文は DB に持たないため、ここに出るのはファイル名と状態だけです。人の投稿は配送試行として記録され、分類は文脈から自動で付きます。</p>
    </header>
    <div className="board-feed">
      {items.map(item => item.marker
        ? <div key={item.key} className="board-marker">{item.marker}</div>
        : <div key={item.key}>{item.node}</div>)}
      {!items.length && <p className="board-empty">この依頼の履歴はまだありません。</p>}
    </div>
    <div className="board-composer">
      <label className="board-composer-label" htmlFor="board-composer">#{task.id} へ投稿 (配送試行として記録されます)</label>
      <textarea id="board-composer" rows={3} value={draft} disabled={sending}
        onChange={event => setDraft(event.target.value)} placeholder="start a thread…" />
      {postError && <p className="board-post-error" role="alert">{postError}</p>}
      <div className="board-composer-row">
        <span>宛先は所属記録上の現在の担当 worker です。担当不在の間は未受領のまま残ります。</span>
        <button type="button" className="board-btn" disabled={!draft.trim() || sending} onClick={send}>send</button>
      </div>
    </div>
  </div>;
}

export default function Board({board, lanes, decisions, loading, error, onOpenDecision}) {
  const [view, setView] = useState('activity');
  const [selectedRun, setSelectedRun] = useState(null);
  const [threadTask, setThreadTask] = useState(null);
  const tasks = allTasks(lanes);
  const unreceivedByTask = new Map();
  for (const post of board?.posts || []) {
    if (post.state === 'unreceived') unreceivedByTask.set(post.task_id, (unreceivedByTask.get(post.task_id) || 0) + 1);
  }
  const openThread = taskId => { setThreadTask(taskId); setView('thread'); };
  return <div className="board">
    <aside className="board-side">
      <div className="board-ws">
        <div>
          <div className="board-ws-name">cue-board</div>
          <div className="board-ws-url mono">127.0.0.1:4317</div>
        </div>
        <span className="board-live">live</span>
      </div>
      <nav aria-label="Board のビュー">
        <button type="button" className="board-nav" aria-selected={view === 'activity'} onClick={() => setView('activity')}>
          activity
          {board && board.unread.posts + board.unread.decisions > 0 &&
            <span className="board-hot">{board.unread.posts + board.unread.decisions}</span>}
        </button>
        <button type="button" className="board-nav" aria-selected={view === 'runs'} onClick={() => setView('runs')}>
          runs<span className="board-count">{board ? board.runs.filter(run => !run.closed_at).length : 0}</span>
        </button>
      </nav>
      <div className="board-side-label">channels (依頼)</div>
      <div className="board-chans">
        {tasks.map(task => <button type="button" key={task.id}
          className={`board-chan ${view === 'thread' && threadTask === task.id ? 'active' : ''}`}
          onClick={() => openThread(task.id)}>
          <span className="board-hash">#</span>
          <span className="board-cname">{task.id}</span>
          <span className={`board-dot ${['human_decision', 'human_action'].includes(task.lane) ? 'wait' : task.lane === 'done' ? 'done' : 'run'}`} />
          {unreceivedByTask.get(task.id) > 0 && <span className="board-bdg">{unreceivedByTask.get(task.id)}</span>}
        </button>)}
        {!tasks.length && <p className="board-empty">依頼はありません。</p>}
      </div>
      <div className="board-side-foot">
        <span className="board-avatar">人</span>
        <span>as 人 (投稿者)</span>
        {board && <span className="board-count">未読 {board.unread.posts + board.unread.decisions}</span>}
      </div>
    </aside>
    <main className="board-main">
      {error && <p className="board-post-error" role="alert">{error}</p>}
      {!board ? <p className="board-empty">{loading ? '読み込んでいます。' : '表示するデータがありません。'}</p>
        : view === 'activity' ? <>
            <h2 className="board-view-title">activity <span>— 全依頼横断の最新</span></h2>
            <Activity board={board} lanes={lanes} decisions={decisions} onOpenThread={openThread} onOpenDecision={onOpenDecision} />
          </>
        : view === 'runs' ? <>
            <h2 className="board-view-title">runs <span>— 所属マップ (開いている run と割り当て)</span></h2>
            <RunMap board={board} lanes={lanes} selectedRun={selectedRun} onSelectRun={setSelectedRun} />
          </>
        : threadTask ? <Thread taskId={threadTask} lanes={lanes} onOpenDecision={onOpenDecision} />
        : <p className="board-empty">左の channels から依頼を選んでください。</p>}
    </main>
  </div>;
}
