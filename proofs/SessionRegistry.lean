namespace SessionRegistry

inductive Role where
  | frontdesk
  | worker
  | cue_worker
  deriving DecidableEq

inductive Cli where
  | claude
  | codex
  deriving DecidableEq

inductive CloseReason where
  | normal
  | replaced
  | crashed
  | closed
  deriving DecidableEq

structure NativeKey where
  cli : Cli
  nativeSessionId : String
  deriving DecidableEq

structure SlotKey where
  runId : String
  role : Role
  pane : String
  socket : String
  deriving DecidableEq

structure Closure where
  closedAt : Nat
  reason : CloseReason
  deriving DecidableEq

structure Session where
  id : Nat
  runId : String
  role : Role
  reqId : String
  pane : String
  socket : String
  cli : Cli
  nativeSessionId : String
  openedAt : Nat
  closure : Option Closure
  deriving DecidableEq

structure OpenRequest where
  runId : String
  role : Role
  reqId : String
  pane : String
  socket : String
  cli : Cli
  nativeSessionId : String
  deriving DecidableEq

inductive CloseTarget where
  | byNative (key : NativeKey)
  | bySlot (runId pane socket : String) (role : Option Role := none)
  deriving DecidableEq

structure Registry where
  nextId : Nat
  sessions : List Session
  deriving DecidableEq

def Session.isOpen (s : Session) : Bool :=
  s.closure.isNone

def Session.nativeKey (s : Session) : NativeKey :=
  ⟨s.cli, s.nativeSessionId⟩

def Session.slotKey (s : Session) : SlotKey :=
  ⟨s.runId, s.role, s.pane, s.socket⟩

def OpenRequest.nativeKey (req : OpenRequest) : NativeKey :=
  ⟨req.cli, req.nativeSessionId⟩

def OpenRequest.slotKey (req : OpenRequest) : SlotKey :=
  ⟨req.runId, req.role, req.pane, req.socket⟩

def matchesOpen (target : CloseTarget) (s : Session) : Bool :=
  s.isOpen &&
    match target with
    | .byNative key => s.nativeKey == key
    | .bySlot runId pane socket none =>
        s.runId == runId && s.pane == pane && s.socket == socket
    | .bySlot runId pane socket (some role) =>
        s.slotKey == ⟨runId, role, pane, socket⟩

def empty : Registry := ⟨1, []⟩

def openSession (reg : Registry) (req : OpenRequest) (now : Nat) : Option (Session × Registry) :=
  if reg.sessions.any (fun s => s.isOpen && (s.nativeKey == req.nativeKey || s.slotKey == req.slotKey)) then
    none
  else
    let created : Session := {
      id := reg.nextId
      runId := req.runId
      role := req.role
      reqId := req.reqId
      pane := req.pane
      socket := req.socket
      cli := req.cli
      nativeSessionId := req.nativeSessionId
      openedAt := now
      closure := none
    }
    some (created, ⟨reg.nextId + 1, created :: reg.sessions⟩)

def closeMatching (target : CloseTarget) (now : Nat) (reason : CloseReason) (sessions : List Session) :
    List Session :=
  sessions.map (fun s =>
    if matchesOpen target s then { s with closure := some ⟨now, reason⟩ } else s)

def closeSession (reg : Registry) (target : CloseTarget) (now : Nat) (reason : CloseReason) :
    Option (Session × Registry) :=
  match reg.sessions.filter (matchesOpen target) with
  | [orig] =>
      let closed := { orig with closure := some ⟨now, reason⟩ }
      some (closed, ⟨reg.nextId, closeMatching target now reason reg.sessions⟩)
  | _ => none

def Compatible (a b : Session) : Prop :=
  a.id ≠ b.id ∧
  (a.isOpen = true → b.isOpen = true → a.nativeKey ≠ b.nativeKey) ∧
  (a.isOpen = true → b.isOpen = true → a.slotKey ≠ b.slotKey)

def WellFormed (reg : Registry) : Prop :=
  (∀ s ∈ reg.sessions, s.id < reg.nextId) ∧
  reg.sessions.Pairwise Compatible

theorem emptyWellFormed : WellFormed empty := by
  refine ⟨?_, List.Pairwise.nil⟩
  intro _ h
  cases h

private theorem compatibleSymm {a b : Session} (h : Compatible a b) : Compatible b a := by
  refine ⟨Ne.symm h.1, ?_, ?_⟩
  · intro hb ha eq
    exact h.2.1 ha hb eq.symm
  · intro hb ha eq
    exact h.2.2 ha hb eq.symm

private theorem pairwiseRel {α : Type} {R : α → α → Prop}
    (hSymm : ∀ {x y}, R x y → R y x) :
    ∀ {xs : List α}, xs.Pairwise R → ∀ {a b}, a ∈ xs → b ∈ xs → a ≠ b → R a b
  | [], _, _, _, ha, _, _ => by cases ha
  | x :: xs, hPw, a, b, ha, hb, hNe => by
      rcases List.pairwise_cons.mp hPw with ⟨hHead, hTail⟩
      rcases List.mem_cons.mp ha with rfl | ha'
      · rcases List.mem_cons.mp hb with rfl | hb'
        · exact absurd rfl hNe
        · exact hHead b hb'
      · rcases List.mem_cons.mp hb with rfl | hb'
        · exact hSymm (hHead a ha')
        · exact pairwiseRel hSymm hTail ha' hb' hNe

theorem activeNativeUnique {reg : Registry} (hWf : WellFormed reg)
    {a b : Session} (ha : a ∈ reg.sessions) (hb : b ∈ reg.sessions)
    (haOpen : a.isOpen = true) (hbOpen : b.isOpen = true)
    (hKey : a.nativeKey = b.nativeKey) : a = b := by
  by_cases hEq : a = b
  · exact hEq
  · exact False.elim ((pairwiseRel compatibleSymm hWf.2 ha hb hEq).2.1 haOpen hbOpen hKey)

theorem activeSlotUnique {reg : Registry} (hWf : WellFormed reg)
    {a b : Session} (ha : a ∈ reg.sessions) (hb : b ∈ reg.sessions)
    (haOpen : a.isOpen = true) (hbOpen : b.isOpen = true)
    (hKey : a.slotKey = b.slotKey) : a = b := by
  by_cases hEq : a = b
  · exact hEq
  · exact False.elim ((pairwiseRel compatibleSymm hWf.2 ha hb hEq).2.2 haOpen hbOpen hKey)

theorem duplicateOpenNativeRejected {reg : Registry} {req : OpenRequest} {now : Nat}
    {s : Session} (hs : s ∈ reg.sessions) (hOpen : s.isOpen = true)
    (hKey : s.nativeKey = req.nativeKey) :
    openSession reg req now = none := by
  have hAny : reg.sessions.any
      (fun x => x.isOpen && (x.nativeKey == req.nativeKey || x.slotKey == req.slotKey)) = true := by
    rw [List.any_eq_true]
    exact ⟨s, hs, by simp [hOpen, hKey]⟩
  simp [openSession, hAny]

theorem duplicateOpenSlotRejected {reg : Registry} {req : OpenRequest} {now : Nat}
    {s : Session} (hs : s ∈ reg.sessions) (hOpen : s.isOpen = true)
    (hKey : s.slotKey = req.slotKey) :
    openSession reg req now = none := by
  have hAny : reg.sessions.any
      (fun x => x.isOpen && (x.nativeKey == req.nativeKey || x.slotKey == req.slotKey)) = true := by
    rw [List.any_eq_true]
    exact ⟨s, hs, by simp [hOpen, hKey]⟩
  simp [openSession, hAny]

theorem openPreservesWellFormed {reg reg' : Registry} {req : OpenRequest} {now : Nat}
    {created : Session} (hWf : WellFormed reg)
    (hOpen : openSession reg req now = some (created, reg')) :
    WellFormed reg' := by
  unfold openSession at hOpen
  split at hOpen
  · contradiction
  · rename_i hNoConflict
    injection hOpen with hPair
    cases hPair
    have hNo : ∀ s ∈ reg.sessions,
        ¬ (s.isOpen = true ∧ (s.nativeKey = req.nativeKey ∨ s.slotKey = req.slotKey)) := by
      intro s hs hBad
      apply hNoConflict
      rw [List.any_eq_true]
      rcases hBad with ⟨hsOpen, hNative | hSlot⟩
      · exact ⟨s, hs, by simp [hsOpen, hNative]⟩
      · exact ⟨s, hs, by simp [hsOpen, hSlot]⟩
    refine ⟨?_, ?_⟩
    · intro s hs
      rcases List.mem_cons.mp hs with rfl | hs'
      · exact Nat.lt_succ_self reg.nextId
      · exact Nat.lt_trans (hWf.1 s hs') (Nat.lt_succ_self reg.nextId)
    · apply List.pairwise_cons.mpr
      refine ⟨?_, hWf.2⟩
      intro s hs
      refine ⟨?_, ?_, ?_⟩
      · exact Nat.ne_of_gt (hWf.1 s hs)
      · intro _ hsOpen hEq
        exact hNo s hs ⟨hsOpen, Or.inl hEq.symm⟩
      · intro _ hsOpen hEq
        exact hNo s hs ⟨hsOpen, Or.inr hEq.symm⟩

private theorem closedMapCompatible {target : CloseTarget} {now : Nat} {reason : CloseReason}
    {a b : Session} (h : Compatible a b) :
    Compatible
      (if matchesOpen target a then { a with closure := some ⟨now, reason⟩ } else a)
      (if matchesOpen target b then { b with closure := some ⟨now, reason⟩ } else b) := by
  by_cases ha : matchesOpen target a = true <;> by_cases hb : matchesOpen target b = true
  · simp [ha, hb, Compatible, Session.isOpen, h.1]
  · simp [ha, hb, Compatible, Session.isOpen, h.1]
  · simp [ha, hb, Compatible, Session.isOpen, h.1]
  · simp [ha, hb, h]

private theorem pairwiseMap {α β : Type} {R : α → α → Prop} {S : β → β → Prop} {f : α → β}
    (hPres : ∀ a b, R a b → S (f a) (f b)) :
    ∀ {xs : List α}, xs.Pairwise R → (xs.map f).Pairwise S
  | [], _ => List.Pairwise.nil
  | _ :: _, hPw => by
      rcases List.pairwise_cons.mp hPw with ⟨hHead, hTail⟩
      apply List.pairwise_cons.mpr
      refine ⟨?_, pairwiseMap hPres hTail⟩
      intro y hy
      rcases List.mem_map.mp hy with ⟨z, hz, rfl⟩
      exact hPres _ _ (hHead z hz)

theorem closePreservesWellFormed {reg reg' : Registry} {target : CloseTarget}
    {now : Nat} {reason : CloseReason} {closed : Session}
    (hWf : WellFormed reg) (hClose : closeSession reg target now reason = some (closed, reg')) :
    WellFormed reg' := by
  unfold closeSession at hClose
  split at hClose
  · rename_i orig hFilter
    injection hClose with hPair
    cases hPair
    refine ⟨?_, ?_⟩
    · intro s hs
      rcases List.mem_map.mp hs with ⟨orig', hOrig', rfl⟩
      by_cases hM : matchesOpen target orig' = true
      · simp [hM, hWf.1 orig' hOrig']
      · simp [hM, hWf.1 orig' hOrig']
    · exact pairwiseMap (fun _ _ h => closedMapCompatible h) hWf.2
  · contradiction

theorem closePreservesIdentity {reg reg' : Registry} {target : CloseTarget}
    {now : Nat} {reason : CloseReason} {closed : Session}
    (hClose : closeSession reg target now reason = some (closed, reg')) :
    ∃ orig ∈ reg.sessions,
      orig.closure = none ∧
      matchesOpen target orig = true ∧
      closed = { orig with closure := some ⟨now, reason⟩ } ∧
      closed.id = orig.id ∧
      closed.runId = orig.runId ∧
      closed.role = orig.role ∧
      closed.reqId = orig.reqId ∧
      closed.pane = orig.pane ∧
      closed.socket = orig.socket ∧
      closed.cli = orig.cli ∧
      closed.nativeSessionId = orig.nativeSessionId ∧
      closed.openedAt = orig.openedAt := by
  unfold closeSession at hClose
  split at hClose
  · rename_i orig hFilter
    injection hClose with hPair
    cases hPair
    have hMemFilter : orig ∈ reg.sessions.filter (matchesOpen target) := by
      rw [hFilter]
      simp
    rcases List.mem_filter.mp hMemFilter with ⟨hOrigMem, hMatches⟩
    have hOpen : orig.isOpen = true := by
      unfold matchesOpen at hMatches
      exact (Bool.and_eq_true _ _ |>.mp hMatches).1
    have hNone : orig.closure = none := by
      unfold Session.isOpen at hOpen
      cases hc : orig.closure with
      | none => rfl
      | some _ => simp [hc] at hOpen
    exact ⟨orig, hOrigMem, hNone, hMatches, rfl, rfl, rfl, rfl, rfl, rfl, rfl, rfl, rfl, rfl⟩
  · contradiction

theorem closedSessionImmutableOnOpen {reg reg' : Registry} {req : OpenRequest}
    {now : Nat} {created s : Session}
    (hs : s ∈ reg.sessions) (_hClosed : s.isOpen = false)
    (hOpen : openSession reg req now = some (created, reg')) :
    s ∈ reg'.sessions := by
  unfold openSession at hOpen
  split at hOpen
  · contradiction
  · injection hOpen with hPair
    cases hPair
    exact List.mem_cons_of_mem _ hs

theorem closedSessionImmutableOnClose {reg reg' : Registry} {target : CloseTarget}
    {now : Nat} {reason : CloseReason} {closed s : Session}
    (hs : s ∈ reg.sessions) (hClosed : s.isOpen = false)
    (hClose : closeSession reg target now reason = some (closed, reg')) :
    s ∈ reg'.sessions := by
  unfold closeSession at hClose
  split at hClose
  · injection hClose with hPair
    cases hPair
    unfold closeMatching
    have hNoMatch : matchesOpen target s = false := by
      simp [matchesOpen, hClosed]
    exact List.mem_map.mpr ⟨s, hs, by simp [hNoMatch]⟩
  · contradiction

theorem closeUnknownRejected {reg : Registry} {target : CloseTarget}
    {now : Nat} {reason : CloseReason}
    (hNone : ∀ s ∈ reg.sessions, matchesOpen target s = false) :
    closeSession reg target now reason = none := by
  have hEmpty : reg.sessions.filter (matchesOpen target) = [] := by
    rw [List.filter_eq_nil_iff]
    intro s hs
    simp [hNone s hs]
  simp [closeSession, hEmpty]

theorem duplicateCloseRejected {reg reg' : Registry} {target : CloseTarget}
    {now₁ now₂ : Nat} {reason₁ reason₂ : CloseReason} {closed : Session}
    (hClose : closeSession reg target now₁ reason₁ = some (closed, reg')) :
    closeSession reg' target now₂ reason₂ = none := by
  apply closeUnknownRejected
  unfold closeSession at hClose
  split at hClose
  · injection hClose with hPair
    cases hPair
    intro s hs
    rcases List.mem_map.mp hs with ⟨orig, _, rfl⟩
    split
    · simp [matchesOpen, Session.isOpen]
    · rename_i hM
      exact Bool.eq_false_iff.mpr hM
  · contradiction

#guard (do
  let req1 : OpenRequest := {
    runId := "run-1", role := .frontdesk, reqId := "",
    pane := "w1:p1", socket := "/tmp/h.sock",
    cli := .claude, nativeSessionId := "uuid-claude-1"
  }
  let (s1, r1) ← openSession empty req1 100
  guard (s1.closure == none && s1.openedAt == 100)
  -- Duplicate native session id (even on another pane/run) is rejected
  guard (openSession r1 { req1 with runId := "run-2", pane := "w2:p1" } 101 == none)
  -- Duplicate active slot (even with another native session id) is rejected
  guard (openSession r1 { req1 with nativeSessionId := "uuid-claude-2" } 101 == none)
  -- Opening a Codex worker on a different slot succeeds
  let req2 : OpenRequest := {
    runId := "run-1", role := .worker, reqId := "req-42",
    pane := "w1:p2", socket := "/tmp/w.sock",
    cli := .codex, nativeSessionId := "uuid-codex-1"
  }
  let (s2, r2) ← openSession r1 req2 105
  guard (s2.cli == .codex && s2.reqId == "req-42")
  -- Closing frontdesk by native key preserves identity fields
  let (c1, r3) ← closeSession r2 (.byNative ⟨.claude, "uuid-claude-1"⟩) 120 .replaced
  guard (c1.id == s1.id && c1.runId == "run-1" && c1.openedAt == 100 && c1.closure == some ⟨120, .replaced⟩)
  -- Duplicate close is rejected
  guard (closeSession r3 (.byNative ⟨.claude, "uuid-claude-1"⟩) 121 .normal == none)
  -- Closing unknown session is rejected
  guard (closeSession r3 (.byNative ⟨.claude, "unknown"⟩) 121 .normal == none)
  -- Slot can be reopened after close, and closed record remains immutable
  let (s3, r4) ← openSession r3 { req1 with nativeSessionId := "uuid-claude-2" } 125
  guard (s3.id != s1.id && r4.sessions.contains c1)
  -- Closing worker by (runId, pane, socket) succeeds and rejects duplicate close
  let (c2, r5) ← closeSession r4 (.bySlot "run-1" "w1:p2" "/tmp/w.sock") 130 .normal
  guard (c2.id == s2.id && c2.closure == some ⟨130, .normal⟩)
  guard (closeSession r5 (.bySlot "run-1" "w1:p2" "/tmp/w.sock") 131 .crashed == none)
  some r5.sessions.length) == some 3

end SessionRegistry
