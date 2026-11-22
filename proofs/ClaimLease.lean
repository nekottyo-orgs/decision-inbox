namespace DecisionInbox

inductive State where
  | pending (lastClaim : Nat)
  | processing (currentClaim leaseUntil : Nat)
  | stopped (lastClaim : Nat)
  | completed (lastClaim : Nat)
  deriving DecidableEq

def claim (now duration : Nat) (state : State) : Option State :=
  if duration = 0 then none else
    match state with
    | .pending last => some (.processing (last + 1) (now + duration))
    | .processing last expires =>
        if expires ≤ now then some (.processing (last + 1) (now + duration)) else none
    | _ => none

-- A failed commit preserves the previous row. A successful commit leaves
-- the new row in place even if the lease expires while waiting for it.
def finishClaim (committed : Bool) (now : Nat) (previous : State)
    (token expires : Nat) : State × Bool :=
  if committed then (.processing token expires, decide (now < expires))
  else (previous, false)

def active (now token : Nat) (state : State) : Prop :=
  match state with
  | .processing current expires => token = current ∧ now < expires
  | _ => False

instance (now token : Nat) (state : State) : Decidable (active now token state) := by
  cases state <;> unfold active <;> infer_instance

def complete (now token : Nat) (state : State) : Option State :=
  if active now token state then some (.completed token) else none

def report (now token : Nat) (state : State) : Option State :=
  if active now token state then some (.stopped token) else none

def renew (now token duration : Nat) (state : State) : Option State :=
  match state with
  | .processing current expires =>
      if active now token state ∧ duration > 0 then
        some (.processing current (max expires (now + duration)))
      else none
  | _ => none

-- For this answer, a report identifies the claim that stopped it.
def retry (reportClaim : Nat) (state : State) : Option State :=
  match state with
  | .stopped last => if reportClaim = last then some (.pending last) else none
  | _ => none

theorem oneActiveToken (h₁ : active now first state) (h₂ : active now second state) :
    first = second := by
  cases state with
  | processing current expires => exact h₁.1.trans h₂.1.symm
  | _ => contradiction

theorem liveClaimCannotBeReplaced (live : now < expires) :
    claim now duration (.processing current expires) = none := by
  simp [claim, Nat.not_le_of_lt live]

theorem expiredClaimGetsFreshToken (expired : expires ≤ now) (positive : duration > 0) :
    claim now duration (.processing current expires) =
      some (.processing (current + 1) (now + duration)) := by
  simp [claim, expired, Nat.ne_of_gt positive]

theorem failedCommitPreservesClaim :
    finishClaim false now previous token expires = (previous, false) := by
  rfl

theorem liveCommittedClaimSucceeds (live : now < expires) :
    finishClaim true now previous token expires = (.processing token expires, true) := by
  simp [finishClaim, live]

theorem expiredCommittedClaimCanBeReclaimed (expired : expires ≤ now) (positive : duration > 0) :
    finishClaim true now previous token expires = (.processing token expires, false) ∧
    claim now duration (finishClaim true now previous token expires).1 =
      some (.processing (token + 1) (now + duration)) := by
  simp [finishClaim, Nat.not_lt_of_ge expired, expiredClaimGetsFreshToken expired positive]

theorem expiredOrSupersededTokenRejected
    (invalid : expires ≤ now ∨ token ≠ current) :
    complete now token (.processing current expires) = none ∧
    report now token (.processing current expires) = none ∧
    renew now token duration (.processing current expires) = none := by
  have inactive : ¬ active now token (.processing current expires) := by
    intro h
    rcases invalid with expired | superseded
    · exact (Nat.not_lt_of_ge expired) h.2
    · exact superseded h.1
  simp [complete, report, renew, inactive]

theorem renewKeepsTokenAndDoesNotShorten (live : now < expires) (positive : duration > 0) :
    let deadline := max expires (now + duration)
    renew now current duration (.processing current expires) =
      some (.processing current deadline) ∧ expires ≤ deadline := by
  simp [renew, active, live, positive, Nat.le_max_left]

theorem stoppedNeedsExplicitRetry :
    claim now duration (.stopped last) = none := by
  simp [claim]

theorem staleReportCannotRetry (stale : reportClaim ≠ last) :
    retry reportClaim (.stopped last) = none := by
  simp [retry, stale]

theorem completedIsTerminal :
    claim now duration (.completed last) = none ∧
    complete now token (.completed last) = none ∧
    report now token (.completed last) = none ∧
    renew now token duration (.completed last) = none ∧
    retry reportClaim (.completed last) = none := by
  simp [claim, complete, report, renew, active, retry]

#guard (do
  let first ← claim 1000 10 (.pending 0)
  guard (claim 1009 10 first == none)
  guard (complete 1010 1 first == none)
  let second ← claim 1010 30 first
  guard (complete 1010 1 second == none)
  let renewed ← renew 1011 2 1 second
  guard (renewed == second)
  let stopped ← report 1011 2 renewed
  guard (retry 1 stopped == none)
  let pending ← retry 2 stopped
  let third ← claim 1012 10 pending
  guard (complete 1012 2 third == none)
  complete 1012 3 third) == some (.completed 3)

end DecisionInbox
