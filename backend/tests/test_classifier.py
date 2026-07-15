from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

from backend.classifier import RULES, classify_file, configure_model, extract_model_features
from backend.taxonomy import DataLevel, MaterialState, Modality


class ClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, relative: str, content: str | bytes = "") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def test_sem_header_and_metadata_tokens(self) -> None:
        path = self.write(
            "2 PA ADR Recycle/新料/SEM/260402/E3-2/1(x40).txt",
            "[SemImageFile]\nInstructName=TM4000\nFormat=tif\n",
        )
        result = classify_file(path)
        self.assertEqual(result.label, Modality.SEM)
        self.assertGreaterEqual(result.confidence, 0.99)
        self.assertEqual(result.metadata.sample, "E3")
        self.assertEqual(result.metadata.date, "2026-04-02")
        self.assertEqual(result.metadata.material, MaterialState.VIRGIN)

    def test_sem_tiff_uses_same_stem_sidecar(self) -> None:
        path = self.write("batch/1(x40).tif", b"II*\x00payload")
        result = classify_file(path, sibling_names=["1(x40).tif", "1(x40).txt"])
        self.assertEqual(result.label, Modality.SEM)
        self.assertEqual(result.metadata.lifecycle, DataLevel.RAW)

    def test_utf16_instrument_sidecar_is_decoded(self) -> None:
        path = self.write(
            "incoming/measurement.txt",
            "[SemImageFile]\r\nInstructName=TM4000\r\n".encode("utf-16"),
        )
        result = classify_file(path)
        self.assertEqual(result.label, Modality.SEM)
        self.assertGreaterEqual(result.confidence, 0.99)

    def test_tensile_header_and_native_export_lifecycle(self) -> None:
        path = self.write(
            "新料/tensile/260402/E4.is_tens_Exports/E4_1_1.csv",
            "结果表格 1\n,拉伸应力 在 断裂,拉伸应变 在 断裂\n,(MPa),(%)\n",
        )
        result = classify_file(path)
        self.assertEqual(result.label, Modality.TENSILE)
        self.assertEqual(result.metadata.sample, "E4")
        self.assertEqual(result.metadata.lifecycle, DataLevel.NATIVE_EXPORT)

    def test_rheology_header(self) -> None:
        path = self.write(
            "incoming/run.csv",
            "Project:\tFrequency Sweep\nInterval data:\tAngular Frequency\tStorage Modulus\tLoss Modulus\tComplex Viscosity\n",
        )
        result = classify_file(path)
        self.assertEqual(result.label, Modality.RHEOLOGY)
        self.assertFalse(result.conflict)

    def test_torque_header_and_embedded_date(self) -> None:
        path = self.write(
            "incoming/A0ZN0.txt",
            "Index\tTemp. Front Top\tScrew Torque\tScrew Speed\tMelt Viscosity\tPressure Transducer\n"
            "Index counts every 1 s\tStarttime is 07 June 2026 - 12:41:13\n",
        )
        result = classify_file(path)
        self.assertEqual(result.label, Modality.TORQUE)
        self.assertEqual(result.metadata.sample, "A0ZN0")
        self.assertEqual(result.metadata.date, "2026-06-07")

    def test_ftir_dense_numeric_trace(self) -> None:
        rows = "\n".join(f"{400 + index * 0.5:.3f},{82 + index * 0.01:.4f}" for index in range(30))
        result = classify_file(self.write("incoming/spectrum.csv", rows))
        self.assertEqual(result.label, Modality.FTIR)
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_simulation_native_extension(self) -> None:
        result = classify_file(self.write("incoming/model.rfn", b"\x01"))
        self.assertEqual(result.label, Modality.SIMULATION)

    def test_impact_sibling_groups_photos(self) -> None:
        path = self.write("incoming/IMG_4146.jpeg", b"jpeg")
        result = classify_file(path, sibling_names=["IMG_4146.jpeg", "impact strength.xlsx"])
        self.assertEqual(result.label, Modality.IMPACT)

    def test_rare_categories_use_auditable_source_folder_hints(self) -> None:
        gpc = classify_file(self.write("GPC/8.xlsx", b"PK\x03\x04"))
        optical = classify_file(self.write("optical image/IMG_1.jpeg", b"jpeg"))
        reference = classify_file(self.write("1 Reference/paper.pdf", b"%PDF-1.7"))
        self.assertEqual(gpc.label, Modality.GPC)
        self.assertEqual(optical.label, Modality.OPTICAL)
        self.assertEqual(reference.label, Modality.REFERENCE)
        self.assertIn("source-folder", gpc.method)

    def test_content_folder_disagreement_is_a_conflict(self) -> None:
        path = self.write(
            "FTIR/A0ZN0.txt",
            "Screw Torque\tScrew Speed\tMelt Viscosity\tPressure Transducer\tTemp. Front\n",
        )
        result = classify_file(path)
        self.assertEqual(result.label, Modality.TORQUE)
        self.assertTrue(result.conflict)
        self.assertLessEqual(result.confidence, 0.79)

    def test_workstream_name_does_not_imply_recycled_material(self) -> None:
        path = self.write("2 PA ADR Recycle/unknown.csv", "a,b\n")
        result = classify_file(path)
        self.assertEqual(result.metadata.material, MaterialState.UNKNOWN)

    def test_model_features_exclude_filename_and_directories(self) -> None:
        secret = "LABEL_SHOULD_NOT_LEAK"
        path = self.write(f"SEM/{secret}/tensile-name.csv", "alpha,beta\n1,2\n")
        features = extract_model_features(path)
        serialized = repr(features)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("tensile-name", serialized)
        self.assertNotIn(str(path.parent), serialized)

    def test_to_dict_contract_and_rule_registry(self) -> None:
        result = classify_file(self.write("incoming/unknown.bin", b"binary"))
        payload = result.to_dict()
        self.assertEqual(
            set(payload),
            {"label", "confidence", "method", "evidence", "conflict", "metadata"},
        )
        self.assertEqual(payload["label"], "UNKNOWN")
        self.assertTrue(RULES)
        self.assertTrue(all(rule.to_dict()["id"] for rule in RULES))

    @unittest.skipUnless(
        importlib.util.find_spec("sklearn") and importlib.util.find_spec("joblib"),
        "optional model-training dependencies are not installed",
    )
    def test_optional_trainer_writes_a_loadable_artifact(self) -> None:
        from backend.train_model import train_from_manifest

        rows: list[tuple[Path, str, str]] = []
        for index in range(3):
            sem = self.write(
                f"training/sem-{index}.txt",
                f"[SemImageFile]\nInstructName=TM4000\nDataNumber={index}\n",
            )
            torque = self.write(
                f"training/torque-{index}.txt",
                "Screw Torque\tScrew Speed\tMelt Viscosity\tPressure Transducer\tTemp. Front\n",
            )
            rows.extend(((sem, "SEM", f"sem-{index}"), (torque, "TORQUE", f"torque-{index}")))
        manifest = self.root / "labels.csv"
        manifest.write_text(
            "path,label,group_id\n"
            + "\n".join(f'"{path}",{label},{group}' for path, label, group in rows),
            encoding="utf-8",
        )
        artifact = self.root / "classifier.joblib"
        report = train_from_manifest(manifest, artifact, evaluate=False)
        self.assertEqual(report["rows"], 6)
        self.assertTrue(artifact.is_file())
        configure_model(artifact)
        try:
            prediction = classify_file(self.write("incoming/fallback.txt", "unseen content\n"))
            self.assertIn(prediction.label, {Modality.SEM, Modality.TORQUE})
            self.assertTrue(prediction.method.startswith("model:"))
        finally:
            configure_model(None)


if __name__ == "__main__":
    unittest.main()
