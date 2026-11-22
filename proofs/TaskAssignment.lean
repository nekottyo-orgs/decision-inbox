namespace TaskAssignment

structure Assignment where
  lane : String
  pane : String
  machine : String
  deriving DecidableEq

structure Scope where
  run : String
  request : String
  kind : String
  deriving DecidableEq

structure Task where
  assignment : Assignment
  scope : Scope
  remaining : String
  revision : Nat
  deriving DecidableEq

def reconcile (current : Task) (expected replacement : Assignment) (scope : Option Scope := none) : Option Task :=
  if current.assignment = expected ∧ (scope = none ∨ scope = some current.scope) then
    if current.assignment = replacement then some current
    else some { current with assignment := replacement, revision := current.revision + 1 }
  else none

theorem mismatchCannotWrite (current : Task) (expected replacement : Assignment)
    (mismatch : current.assignment ≠ expected) :
    reconcile current expected replacement = none := by
  simp [reconcile, mismatch]

theorem changedScopeCannotWrite (current : Task) (expected replacement : Assignment) (oldScope : Scope)
    (mismatch : oldScope ≠ current.scope) :
    reconcile current expected replacement (some oldScope) = none := by
  simp [reconcile, mismatch]

theorem successfulReconcileReplacesOnlyAssignment (current : Task) (expected replacement : Assignment)
    (matched : current.assignment = expected) (changed : current.assignment ≠ replacement) :
    reconcile current expected replacement (some current.scope) =
      some ⟨replacement, current.scope, current.remaining, current.revision + 1⟩ := by
  simp [reconcile, matched, show expected ≠ replacement from matched ▸ changed]

theorem identicalAssignmentLeavesRowUnchanged (current : Task) :
    reconcile current current.assignment current.assignment (some current.scope) = some current := by
  simp [reconcile]

end TaskAssignment
