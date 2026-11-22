namespace DecisionSnapshot

structure Decision where
  body : String
  history : List String

structure ReadTransaction where
  snapshot : Decision
  live : Decision

def firstRead (live : Decision) : ReadTransaction := ⟨live, live⟩

def commit (reader : ReadTransaction) (next : Decision) : ReadTransaction :=
  { reader with live := next }

def listResponse (historyAtFirstRead : List String) (reader : ReadTransaction) :=
  (reader.snapshot.body, historyAtFirstRead.length, historyAtFirstRead)

def detailResponse (bodyAtFirstRead : String) (reader : ReadTransaction) :=
  (bodyAtFirstRead, reader.snapshot.history.length, reader.snapshot.history)

theorem readsKeepSameVersion (initial : Decision) (commits : List Decision) :
    let reader := commits.foldl commit (firstRead initial)
    let expected := (initial.body, initial.history.length, initial.history)
    listResponse initial.history reader = expected ∧ detailResponse initial.body reader = expected := by
  have snapshotPreserved (reader : ReadTransaction) (writes : List Decision) :
      (writes.foldl commit reader).snapshot = reader.snapshot := by
    induction writes generalizing reader with
    | nil => rfl
    | cons next rest ih => exact ih (commit reader next)
  simp [listResponse, detailResponse, snapshotPreserved, firstRead]

end DecisionSnapshot
