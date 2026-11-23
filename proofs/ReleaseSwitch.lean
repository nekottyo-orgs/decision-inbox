namespace ReleaseSwitch

inductive Active where
  | previous
  | candidate
  | foreign
  deriving DecidableEq

inductive Phase where
  | preparing
  | checked
  | replacing
  | checkingLive
  | applied
  | stopped
  | rolledBack
  | lostOwnership
  deriving DecidableEq

structure State where
  phase : Phase
  current : Active
  deriving DecidableEq

def checked : Phase → Bool
  | .checked | .replacing | .checkingLive | .applied | .rolledBack => true
  | _ => false

def liveOutcome : Bool → State
  | true => ⟨.applied, .candidate⟩
  | false => ⟨.rolledBack, .previous⟩

def interruptedOutcome : Active → State
  | .previous => ⟨.stopped, .previous⟩
  | .candidate => ⟨.rolledBack, .previous⟩
  | .foreign => ⟨.lostOwnership, .foreign⟩

-- The lock serializes attempts by the updater. The atomic rename changes the link
-- before control leaves the replacement boundary.
inductive Step : State → State → Prop where
  | preflightPass : Step ⟨.preparing, .previous⟩ ⟨.checked, .previous⟩
  | preflightFail : Step ⟨.preparing, .previous⟩ ⟨.stopped, .previous⟩
  | beginReplace : Step ⟨.checked, .previous⟩ ⟨.replacing, .previous⟩
  | renameLink : Step ⟨.replacing, .previous⟩ ⟨.replacing, .candidate⟩
  | finishReplace : Step ⟨.replacing, .candidate⟩ ⟨.checkingLive, .candidate⟩
  | foreignDuringReplace (link : Active) : Step ⟨.replacing, link⟩ ⟨.replacing, .foreign⟩
  | interruptAtReplace (link : Active) :
      Step ⟨.replacing, link⟩ (interruptedOutcome link)
  | liveProbe (listenerAndWebMatch : Bool) :
      Step ⟨.checkingLive, .candidate⟩ (liveOutcome listenerAndWebMatch)
  | foreignBeforeActivation : Step ⟨.checked, .previous⟩ ⟨.lostOwnership, .foreign⟩
  | foreignWhileChecking : Step ⟨.checkingLive, .candidate⟩ ⟨.lostOwnership, .foreign⟩

inductive Reachable : State → Prop where
  | start : Reachable ⟨.preparing, .previous⟩
  | step : Reachable before → Step before after → Reachable after

theorem activeCandidateWasChecked (state : State) (reachable : Reachable state) :
    state.current = .candidate → checked state.phase = true := by
  cases reachable with
  | start => decide
  | step _ transition =>
      cases transition with
      | preflightPass | preflightFail | beginReplace | renameLink | finishReplace
      | foreignDuringReplace | foreignBeforeActivation | foreignWhileChecking => decide
      | interruptAtReplace link => cases link <;> decide
      | liveProbe matched => cases matched <;> decide

theorem failingPreflightKeepsPrevious {after : State}
    (step : Step ⟨.preparing, .previous⟩ after) (notChecked : checked after.phase = false) :
    after.current = .previous := by
  cases step
  · contradiction
  · rfl

theorem failingLiveCheckRestoresPrevious :
    Step ⟨.checkingLive, .candidate⟩ ⟨.rolledBack, .previous⟩ :=
  .liveProbe false

theorem replacementBoundaryCanObserve (link : Active) : Reachable ⟨.replacing, link⟩ := by
  have prepared : Reachable ⟨.replacing, .previous⟩ :=
    .step (.step .start .preflightPass) .beginReplace
  cases link with
  | previous => exact prepared
  | candidate => exact .step prepared .renameLink
  | foreign => exact .step prepared (.foreignDuringReplace .previous)

theorem interruptedReplaceUsesObservedLink (link : Active) :
    Reachable ⟨.replacing, link⟩ ∧
    match link with
    | .previous => Step ⟨.replacing, link⟩ ⟨.stopped, .previous⟩
    | .candidate => Step ⟨.replacing, link⟩ ⟨.rolledBack, .previous⟩
    | .foreign => Step ⟨.replacing, link⟩ ⟨.lostOwnership, .foreign⟩ := by
  constructor
  · exact replacementBoundaryCanObserve link
  · cases link <;> exact .interruptAtReplace _

theorem unmatchedListenerCannotApply : (liveOutcome false).phase ≠ .applied := by
  decide

theorem foreignIsNotOverwritten {after : State} (step : Step ⟨.lostOwnership, .foreign⟩ after) :
    False := by
  cases step

end ReleaseSwitch
