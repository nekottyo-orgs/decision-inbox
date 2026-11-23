namespace ReleaseSwitch

inductive Active where
  | previous
  | candidate
  | foreign
  deriving DecidableEq

inductive Phase where
  | preparing
  | checked
  | checkingLive
  | applied
  | stopped
  | rolledBack
  | lostOwnership

def active : Phase → Active
  | .checkingLive | .applied => .candidate
  | .lostOwnership => .foreign
  | _ => .previous

def checked : Phase → Bool
  | .checked | .checkingLive | .applied | .rolledBack => true
  | _ => false

-- The lock serializes attempts. The link update itself is an atomic rename.
inductive Step : Phase → Phase → Prop where
  | preflightPass : Step .preparing .checked
  | preflightFail : Step .preparing .stopped
  | activate : Step .checked .checkingLive
  | livePass : Step .checkingLive .applied
  | liveFail : Step .checkingLive .rolledBack
  | foreignBeforeActivation : Step .checked .lostOwnership
  | foreignWhileChecking : Step .checkingLive .lostOwnership

inductive Reachable : Phase → Prop where
  | start : Reachable .preparing
  | step : Reachable before → Step before after → Reachable after

theorem activeCandidateWasChecked (phase : Phase) (_ : Reachable phase) :
    active phase = .candidate → checked phase = true := by
  cases phase <;> decide

theorem failingPreflightKeepsPrevious {after : Phase}
    (step : Step .preparing after) (notChecked : checked after = false) :
    active after = .previous := by
  cases step
  · contradiction
  · rfl

theorem failingLiveCheckRestoresPrevious :
    Step .checkingLive .rolledBack ∧ active .rolledBack = .previous :=
  ⟨.liveFail, rfl⟩

theorem foreignIsNotOverwritten {after : Phase} (step : Step .lostOwnership after) : False := by
  cases step

end ReleaseSwitch
