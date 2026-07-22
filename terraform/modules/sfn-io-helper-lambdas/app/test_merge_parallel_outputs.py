"""Standalone unit test for stage_io.merge_parallel_outputs (fan-out branch merge).
Run: python terraform/modules/sfn-io-helper-lambdas/app/test_merge_parallel_outputs.py
"""
import sys, os, types
from unittest import mock
# stub boto3 so stage_io imports without AWS
b = types.ModuleType("boto3"); b.client = lambda *a, **k: None; b.resource = lambda *a, **k: None
sys.modules["boto3"] = b
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sfn_io_helper import stage_io

# Three Phase-1 download branches: each accumulated only its own outputs.
branches = [
    {"OutputPrefix": "s3://o/", "STAGES_IO_MAP_JSON": "s3://o/m.json",
     "Input": {"CompressNR": {"docker_image_id": "img"}},
     "Result": {"index_generation_download_taxonomy.accession2taxid_pdb": "s3://o/pdb",
                "index_generation_download_taxonomy.accession2taxid_prot": "s3://o/prot"},
     "BatchJobDetails": {"DownloadTaxonomy": {}}},
    {"OutputPrefix": "s3://o/", "STAGES_IO_MAP_JSON": "s3://o/m.json", "Input": {"CompressNR": {}},
     "Result": {"index_generation_download_nt.nt": "s3://o/nt"},
     "BatchJobDetails": {"DownloadNT": {}}},
    {"OutputPrefix": "s3://o/", "STAGES_IO_MAP_JSON": "s3://o/m.json", "Input": {"CompressNR": {}},
     "Result": {"index_generation_download_nr.nr": "s3://o/nr"},
     "BatchJobDetails": {"DownloadNR": {}}},
]
captured = {}
with mock.patch.object(stage_io, "link_outputs", side_effect=lambda s: captured.update(s)):
    merged = stage_io.merge_parallel_outputs({"Branches": branches})

# Result unioned across all three branches (cross-branch visibility restored)
r = merged["Result"]
assert r["index_generation_download_nt.nt"] == "s3://o/nt", r
assert r["index_generation_download_nr.nr"] == "s3://o/nr", r
assert r["index_generation_download_taxonomy.accession2taxid_prot"] == "s3://o/prot", r
# BatchJobDetails unioned
assert set(merged["BatchJobDetails"]) == {"DownloadTaxonomy", "DownloadNT", "DownloadNR"}
# shared keys carried from base branch
assert merged["STAGES_IO_MAP_JSON"] == "s3://o/m.json"
# link_outputs was invoked on the merged state (the reused generic resolver)
assert captured["Result"] is merged["Result"]
# branch inputs NOT mutated destructively (base copied)
assert merged is not branches[0]
print("MERGE UNIT TEST PASSED")
