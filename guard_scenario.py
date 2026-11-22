"""Independent volume-port interleavings and sample data for physical inventory checks."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from archive import Archive, initialize
from copies.physical_store import LocalVolumeStore
from copies.volume import VolumeLocation, VolumeReplyLost, VolumeState
from intake.rows import OriginalRow

TARGET = "GUARD-T"
BACKUP = "GUARD-W"
REQUEST = "GUARD-R"
A = OriginalRow("GUARD-A", "bend/guard/01", 7)
B = OriginalRow("GUARD-B", "bend/guard/02", 11)
C = OriginalRow("GUARD-C", "bend/guard/03", 11)


class CatalogInterrupted(BaseException):
    """Interrupt the caller after the volume committed."""


class ScriptedVolumeStore(LocalVolumeStore):
    """Run a one-shot event at dispatch to the named volume."""

    def __init__(self, path, target, *, before_destroy=None, interruption=None):
        if interruption not in (None, "before", "after", "rollback"):
            raise ValueError("unknown interruption")
        super().__init__(path)
        self.target = target
        self.before_destroy = before_destroy
        self.interruption = interruption

    def destroy(self, volume_id, operation_id, *args, **kwargs):
        if volume_id != self.target:
            return super().destroy(volume_id, operation_id, *args, **kwargs)
        callback = self.before_destroy
        self.before_destroy = None
        if callback is not None:
            callback()
        if self.interruption == "before":
            self.interruption = None
            raise VolumeReplyLost("dispatch was interrupted before the volume received it")
        outcome = super().destroy(volume_id, operation_id, *args, **kwargs)
        if self.interruption in ("after", "rollback") and self.inspect(volume_id) is VolumeState.DESTROYED:
            interruption = self.interruption
            self.interruption = None
            if interruption == "rollback":
                raise CatalogInterrupted("volume committed before the catalog could settle")
            raise VolumeReplyLost("volume committed but its reply was lost")
        return outcome


def prepare(desk, *, provision_target=None):
    """Seed A on both volumes, B on neither, and a same-valued C on W."""
    desk.load("GUARD-ORIGINAL", [A])
    desk.load("GUARD-FEED", [B, C])
    if provision_target is None:
        provision_target = desk.volumes.provision_volume
    provision_target(TARGET, {"0": A})
    desk.register(A.original_id, VolumeLocation(TARGET, "0"), A)
    desk.volumes.provision_volume(BACKUP, {"0": A, "2": C})
    desk.register(A.original_id, VolumeLocation(BACKUP, "0"), A)
    desk.register(C.original_id, VolumeLocation(BACKUP, "2"), C)
    return desk.get("GUARD-ORIGINAL")


def main():
    """Check the fixture and dispatch interruption on an ordinary V1 volume."""
    with TemporaryDirectory(prefix="bend-guard-fixture-") as directory:
        root = Path(directory)
        initialize(root)
        physical_path = root / "volumes.sqlite3"
        with Archive(root) as desk:
            first = prepare(desk)
        with ScriptedVolumeStore(physical_path, TARGET, interruption="before") as port, Archive(root, port=port) as desk:
            early = desk.retire(REQUEST, TARGET)
            if early.state.value != "pending" or port.inspect(TARGET) is not VolumeState.INTACT:
                raise RuntimeError("the pre-dispatch interruption must leave an intact volume")
        with ScriptedVolumeStore(physical_path, TARGET, interruption="after") as port, Archive(root, port=port) as desk:
            late = desk.retire(REQUEST, TARGET)
            if late.state.value != "pending" or port.inspect(TARGET) is not VolumeState.DESTROYED:
                raise RuntimeError("the reply interruption must follow physical destruction")
        with Archive(root) as desk:
            settled = desk.retire(REQUEST, TARGET)
            old = desk.history(first.receipt_id)
            if (settled.state.value != "retired" or settled.operation_id != desk.volumes.destruction_id(TARGET)
                    or settled.evidence != ((A.original_id, VolumeLocation(BACKUP, "0")),)
                    or old != first or old.total != 7):
                raise RuntimeError("the original workflow must settle and preserve the first receipt")
            print(json.dumps({"before_dispatch": early.state.value, "after_commit": late.state.value,
                              "reopened": settled.state.value, "destruction_id": settled.operation_id,
                              "historical_total": old.total}, sort_keys=True))


def demo_inventory():
    """Demonstrate the inventory-guarded volume from a waiting B through reopen."""
    with TemporaryDirectory(prefix="bend-guard-demo-") as directory:
        root = Path(directory)
        initialize(root)
        with Archive(root) as desk:
            def provision_target(volume_id, slots, **kwargs):
                desk.volumes.provision_volume(volume_id, slots, inventory_guard=True, **kwargs)

            first = prepare(desk, provision_target=provision_target)
            desk.volumes.provision_volume(TARGET, {"1": B})  # machine appends, unregistered
            waiting = desk.retire(REQUEST, TARGET)
            if waiting.state.value != "waiting" or waiting.missing != (B.original_id,) \
                    or waiting.unverified_slots != () or desk.volumes.inspect(TARGET) is not VolumeState.INTACT:
                raise RuntimeError("the unregistered B slot must hold the retirement")
            unregistered_inventory = [list(pair) for pair in waiting.inventory]

            location = VolumeLocation(BACKUP, "1")
            desk.volumes.provision_volume(BACKUP, {"1": B})
            desk.register(B.original_id, location, B)
        with ScriptedVolumeStore(root / "volumes.sqlite3", TARGET, interruption="rollback") as port, \
                Archive(root, port=port) as desk:
            try:
                desk.retire(REQUEST, TARGET)
            except CatalogInterrupted:
                pass
            else:
                raise RuntimeError("the catalog interruption must follow the volume commit")
            if desk.volumes.inspect(TARGET) is not VolumeState.DESTROYED:
                raise RuntimeError("the volume must commit independently of the catalog")
        with Archive(root) as reopened:
            settled = reopened.retire(REQUEST, TARGET)
            old = reopened.history(first.receipt_id)
            if (settled.state.value != "retired"
                    or settled.operation_id != reopened.volumes.destruction_id(TARGET)
                    or settled.inventory != (("0", "GUARD-A"), ("1", "GUARD-B"))
                    or settled.evidence != ((A.original_id, VolumeLocation(BACKUP, "0")),
                                            (B.original_id, VolumeLocation(BACKUP, "1")))
                    or old != first or old.total != 7):
                raise RuntimeError("the guarded retirement must settle from the volume record")
            print(json.dumps({
                "waiting": {"state": waiting.state.value, "missing": list(waiting.missing),
                            "unverified_slots": list(waiting.unverified_slots),
                            "inventory": unregistered_inventory},
                "reopened": {"state": settled.state.value, "operation_id": settled.operation_id,
                             "inventory": [list(pair) for pair in settled.inventory],
                             "evidence": [[original_id, list((location.volume_id, location.slot))]
                                          for original_id, location in settled.evidence]},
                "old_receipt": {"receipt_id": old.receipt_id,
                                "readings": [line.reading for line in old.lines], "total": old.total},
            }, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
