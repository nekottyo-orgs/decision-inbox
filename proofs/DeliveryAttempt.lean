namespace DeliveryAttempt

inductive Classification where
  | expected
  | unexpected
  deriving DecidableEq

inductive State where
  | unreceived
  | received
  deriving DecidableEq

structure Target where
  runId : String
  pane : String
  socket : String
  nativeSessionId : String
  deriving DecidableEq

structure Post where
  id : Nat
  taskId : String
  classification : Classification
  state : State
  target : Option Target
  createdAt : Nat
  receivedAt : Option Nat
  receivedBy : Option String
  deriving DecidableEq

/-- 投稿の分類は、対象の依頼が人待ち理由で止まっているかという文脈だけから導く。
    投稿者は選ばない (設計 Q13)。 -/
def classify (waitingForHuman : Bool) : Classification :=
  if waitingForHuman then .expected else .unexpected

/-- 投稿は常に未受領で生まれる。宛先は所属記録上の現在の担当で、担当不在なら none。
    担当不在の投稿も未受領のまま残り、後から受領対象になる (設計 Q12)。 -/
def post (id : Nat) (taskId : String) (waitingForHuman : Bool) (target : Option Target) (now : Nat) : Post :=
  { id, taskId, classification := classify waitingForHuman, state := .unreceived,
    target, createdAt := now, receivedAt := none, receivedBy := none }

/-- 受領は未受領からのみ起きる。分類・宛先・投稿時刻は受領で変わらない。 -/
def receive (now : Nat) (receiver : String) (p : Post) : Option Post :=
  if p.state = .unreceived then
    some { p with state := .received, receivedAt := some now, receivedBy := some receiver }
  else
    none

theorem postIsUnreceived (id : Nat) (taskId : String) (waiting : Bool) (target : Option Target) (now : Nat) :
    (post id taskId waiting target now).state = .unreceived := rfl

theorem postClassificationFromContext (id : Nat) (taskId : String) (target : Option Target) (now : Nat) :
    (post id taskId true target now).classification = .expected ∧
    (post id taskId false target now).classification = .unexpected := by
  exact ⟨rfl, rfl⟩

theorem receiveRequiresUnreceived (now : Nat) (receiver : String) (p p' : Post)
    (done : receive now receiver p = some p') : p.state = .unreceived := by
  unfold receive at done
  split at done
  · assumption
  · simp at done

theorem receivePreservesRecord (now : Nat) (receiver : String) (p p' : Post)
    (done : receive now receiver p = some p') :
    p'.classification = p.classification ∧ p'.taskId = p.taskId ∧
    p'.target = p.target ∧ p'.createdAt = p.createdAt := by
  unfold receive at done
  split at done
  · injection done with hd
    subst hd
    exact ⟨rfl, rfl, rfl, rfl⟩
  · simp at done

theorem receiveSetsReceipt (now : Nat) (receiver : String) (p : Post)
    (unreceived : p.state = .unreceived) :
    receive now receiver p = some { p with state := .received, receivedAt := some now, receivedBy := some receiver } := by
  simp [receive, unreceived]

theorem receivedIsTerminal (now : Nat) (receiver : String) (p : Post)
    (closed : p.state = .received) : receive now receiver p = none := by
  simp [receive, closed]

#guard (do
  let target : Target := ⟨"run-1", "w1:p2", "/tmp/herdr.sock", "uuid-1"⟩
  let first := post 1 "task-1" true (some target) 100
  guard (first.classification == .expected)
  guard (first.state == .unreceived)
  let received ← receive 120 "uuid-1" first
  guard (received.state == .received)
  guard (received.receivedAt == some 120)
  guard (received.classification == .expected)
  guard (receive 130 "uuid-1" received == none)
  let second := post 2 "task-2" false none 140
  guard (second.classification == .unexpected)
  guard (second.target == none)
  let pickedUp ← receive 150 "uuid-9" second
  pure (pickedUp.state == .received)) == some true

end DeliveryAttempt
