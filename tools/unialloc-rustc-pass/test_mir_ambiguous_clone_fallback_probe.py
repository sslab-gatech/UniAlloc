#!/usr/bin/env python3
"""Validate supported plain Clone isolation beside ambiguous Clone fallback."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[2]
RUNNER_SOURCE = Path(__file__).resolve()
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "rustc_driver_mir_ambiguous_clone_fallback_probe"
PROBE_SOURCE = ROOT / "unialloc/src/bin" / f"{PROBE_NAME}.rs"
SOURCE_BINDING_PATHS = (Path("Cargo.toml"), Path("Cargo.lock"), Path("rust-toolchain"), Path("alloc_macros/Cargo.toml"), Path("alloc_macros/src"), Path("unialloc/Cargo.toml"), Path("unialloc/build.rs"), Path("unialloc/src"), PASS_SOURCE.relative_to(ROOT), RUNNER_SOURCE.relative_to(ROOT))
AMBIGUOUS_FUNCTION = "Result::clone"
AMBIGUOUS_STATUS = "semantic_scope_rewrite_skipped_ambiguous_heap_object_type"
PLAIN_CLONE_FUNCTION = "Option::clone"
PLAIN_CLONE_HELPER = "supported_plain_option_clone"
PLAIN_RESULT_CLONE_FUNCTION = "Result::clone"
PLAIN_RESULT_CLONE_HELPER = "supported_plain_result_clone"
WRONG_TYPE_HELPER = "supported_wrong_type_seed_buffer"
APPLIED_STATUSES = {"actual_semantic_scope_enter_exit_rewrite_applied", "semantic_scope_enter_exit_rewrite_planned", "actual_semantic_scope_drop_rewrite_applied", "semantic_scope_drop_rewrite_planned"}
TYPE_ISOLATED = 1 << 0
SUPPORTED_FUNCTIONS = (
    "supported_seed_protected_buffer",
    "supported_recover_protected_buffer",
)

def sha256(path: Path) -> str:
    d=hashlib.sha256()
    with path.open('rb') as h:
        for c in iter(lambda:h.read(1024*1024), b''): d.update(c)
    return d.hexdigest()

def command_text(command: Iterable[str]) -> str: return " ".join(command)

def run_logged(command: List[str], *, label: str, output_dir: Path, timeout: int, env: Dict[str,str]) -> Dict[str,Any]:
    out=output_dir/f"{label}.stdout.txt"; err=output_dir/f"{label}.stderr.txt"
    try:
        r=subprocess.run(command,cwd=ROOT,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False); to=False; rc=r.returncode; so=r.stdout; se=r.stderr
    except subprocess.TimeoutExpired as e:
        to=True; rc=124; so=e.stdout or ''; se=e.stderr or ''
    out.write_text(so,encoding='utf-8'); err.write_text(se,encoding='utf-8')
    return {"command":command,"command_text":command_text(command),"returncode":rc,"timed_out":to,"stdout":str(out),"stderr":str(err)}

def checked_output(command: List[str]) -> str: return subprocess.check_output(command,cwd=ROOT,text=True).strip()

def reject_output_inside_repo(output_dir: Path) -> None:
    r=output_dir.resolve(); root=ROOT.resolve()
    if r==root or root in r.parents: raise AssertionError(f"output directory must be outside repository: {r}")

def default_output_dir() -> Path:
    p=Path(tempfile.gettempdir()).resolve(); reject_output_inside_repo(p)
    return Path(tempfile.mkdtemp(prefix='unialloc-mir-ambiguous-clone-', dir=str(p))).resolve()

def scoped_source_hashes() -> Dict[str,str]:
    files=[]
    for rel in SOURCE_BINDING_PATHS:
        p=ROOT/rel; assert p.exists(), f"source-binding path is missing: {rel}"
        files.extend([c for c in p.rglob('*') if c.is_file()] if p.is_dir() else [p])
    return {str(p.relative_to(ROOT)):sha256(p) for p in sorted(set(files))}

def scoped_source_fingerprint(file_hashes: Dict[str,str]) -> str:
    d=hashlib.sha256()
    for p,h in sorted(file_hashes.items()): d.update(p.encode()); d.update(b'\0'); d.update(h.encode('ascii')); d.update(b'\n')
    return d.hexdigest()

def source_binding_snapshot(toolchain: str, rustc: str) -> Dict[str,Any]:
    status=checked_output(['git','status','--short']); assert not status, f"commit-bound evidence requires a clean working tree:\n{status}"
    hashes=scoped_source_hashes()
    return {"git_head":checked_output(['git','rev-parse','HEAD']),"git_status":status,"clean_head":True,"toolchain":toolchain,"rustc_verbose_version":checked_output([rustc,f'+{toolchain}','-Vv']),"sysroot":checked_output([rustc,f'+{toolchain}','--print','sysroot']),"scoped_paths":[str(p) for p in SOURCE_BINDING_PATHS],"scoped_file_count":len(hashes),"scoped_file_hashes":hashes,"scoped_fingerprint_sha256":scoped_source_fingerprint(hashes)}

def assert_source_binding_stable(start: Dict[str,Any], end: Dict[str,Any]) -> None:
    drift={k:{'start':start.get(k),'end':end.get(k)} for k in start if start.get(k)!=end.get(k)}
    assert not drift, f"source/toolchain binding drifted during probe: {json.dumps(drift,sort_keys=True)}"

def current_rustc_cfg(toolchain: str) -> List[str]:
    n=toolchain.lstrip('+').strip(); return ['--cfg','unialloc_rustc_current'] if n=='nightly' or n.startswith(('nightly-2025','nightly-2026')) else []

def load_runtime_event(stdout_path: Path) -> Dict[str,Any]:
    for raw in stdout_path.read_text(encoding='utf-8').splitlines():
        line=raw.strip()
        if line.startswith('{'):
            v=json.loads(line)
            if isinstance(v,dict) and v.get('source')==PROBE_NAME: return v
    raise AssertionError(f"missing {PROBE_NAME} JSON event in {stdout_path}")

def ambiguous_clone_rows(audit: Dict[str,Any]) -> List[Dict[str,Any]]:
    rows=[]
    for row in audit.get('rewrite_candidates',[]):
        if not isinstance(row,dict): continue
        callee=str(row.get('callee') or '')
        destination=str(row.get('destination_type') or '')
        if 'Clone' in callee and 'clone' in callee and 'Result' in destination and 'ProducerPayload' in destination and 'String' in destination:
            rows.append(row)
    return rows

def probe_function_matches(row: Dict[str,Any], function_name: str) -> bool:
    mir_function=str(row.get('mir_function') or '')
    return mir_function==function_name or mir_function.endswith(f"::{function_name}")

def actual_type_isolated_scope(row: Dict[str,Any], label: str) -> None:
    assert row.get('lowering_kind')=='semantic_scope_enter_exit_rewrite', f"{label} has the wrong lowering kind: {row!r}"
    assert row.get('rewrite_status')=='actual_semantic_scope_enter_exit_rewrite_applied', f"{label} was not actually applied: {row!r}"
    assert str(row.get('replacement_resolution_status') or '').startswith('resolved_unialloc_semantic_scope'), f"{label} did not resolve the UniAlloc semantic scope: {row!r}"
    assert row.get('metadata_pairing_contract')=='semantic_scope_active_metadata', f"{label} has the wrong metadata pairing contract: {row!r}"
    assert int(row.get('flags') or 0)&TYPE_ISOLATED, f"{label} is not type isolated: {row!r}"

def validate_plain_clone_control(audit: Dict[str,Any]) -> Dict[str,Any]:
    rows=[]
    for row in audit.get('rewrite_candidates',[]):
        if not isinstance(row,dict) or not probe_function_matches(row,PLAIN_CLONE_HELPER): continue
        callee=str(row.get('callee') or '')
        destination=str(row.get('destination_type') or '')
        semantic=str(row.get('semantic_object_type') or '')
        if 'Clone' in callee and 'clone' in callee and 'Option' in destination and 'Vec' in destination and 'ProducerPayload' in destination and 'Vec<ProducerPayload' in semantic:
            rows.append(row)
    assert len(rows)==1, f"{PLAIN_CLONE_HELPER} must have exactly one supported Option Clone scope candidate, got {len(rows)}: {rows!r}"
    row=rows[0]
    actual_type_isolated_scope(row, f"{PLAIN_CLONE_HELPER} Option Clone scope")
    type_id=int(row.get('type_id') or 0); module_id=int(row.get('module_id') or 0)
    assert type_id!=0, "supported Option Clone compiler type_id must be nonzero"
    assert module_id!=0, "supported Option Clone module_id must be nonzero"
    return {
        'actual_scope_rows':1,
        'callee':row.get('callee'),
        'destination_type':row.get('destination_type'),
        'semantic_object_type':row.get('semantic_object_type'),
        'rewrite_status':row.get('rewrite_status'),
        'source_span':row.get('source_span'),
        'type_id':type_id,
        'module_id':module_id,
    }

def validate_plain_result_clone_control(audit: Dict[str,Any]) -> Dict[str,Any]:
    rows=[]
    for row in audit.get('rewrite_candidates',[]):
        if not isinstance(row,dict) or not probe_function_matches(row,PLAIN_RESULT_CLONE_HELPER): continue
        callee=str(row.get('callee') or '')
        destination=str(row.get('destination_type') or '')
        semantic=str(row.get('semantic_object_type') or '')
        if 'Clone' in callee and 'clone' in callee and 'Result' in destination and 'ProducerPayload' in destination and 'u8' in destination and 'Vec<ProducerPayload' in semantic:
            rows.append(row)
    assert len(rows)==1, f"{PLAIN_RESULT_CLONE_HELPER} must have exactly one supported Result Clone scope candidate, got {len(rows)}: {rows!r}"
    row=rows[0]
    actual_type_isolated_scope(row, f"{PLAIN_RESULT_CLONE_HELPER} Result Clone scope")
    type_id=int(row.get('type_id') or 0); module_id=int(row.get('module_id') or 0)
    assert type_id!=0, "supported Result Clone compiler type_id must be nonzero"
    assert module_id!=0, "supported Result Clone module_id must be nonzero"
    return {
        'actual_scope_rows':1,
        'callee':row.get('callee'),
        'destination_type':row.get('destination_type'),
        'semantic_object_type':row.get('semantic_object_type'),
        'rewrite_status':row.get('rewrite_status'),
        'source_span':row.get('source_span'),
        'type_id':type_id,
        'module_id':module_id,
    }

def validate_wrong_type_control(audit: Dict[str,Any]) -> Dict[str,Any]:
    rows=[]
    for row in audit.get('rewrite_candidates',[]):
        if not isinstance(row,dict) or not probe_function_matches(row,WRONG_TYPE_HELPER): continue
        text=' '.join(str(row.get(field) or '') for field in ('semantic_object_type','destination_type'))
        if 'with_capacity' in str(row.get('callee') or '') and 'Vec<ConsumerPayload' in text:
            rows.append(row)
    assert len(rows)==1, f"{WRONG_TYPE_HELPER} must have exactly one same-layout Consumer allocation scope candidate, got {len(rows)}: {rows!r}"
    row=rows[0]
    actual_type_isolated_scope(row, f"{WRONG_TYPE_HELPER} Consumer allocation scope")
    type_id=int(row.get('type_id') or 0); module_id=int(row.get('module_id') or 0)
    assert type_id!=0, "same-layout Consumer compiler type_id must be nonzero"
    assert module_id!=0, "same-layout Consumer module_id must be nonzero"
    return {
        'actual_scope_rows':1,
        'semantic_object_type':row.get('semantic_object_type'),
        'rewrite_status':row.get('rewrite_status'),
        'source_span':row.get('source_span'),
        'type_id':type_id,
        'module_id':module_id,
    }

def validate_supported_controls(audit: Dict[str,Any]) -> Dict[str,Any]:
    evidence={}
    all_rows=[]
    for function_name in SUPPORTED_FUNCTIONS:
        function_rows=[
            row for row in audit.get('rewrite_candidates',[])
            if isinstance(row,dict)
            and probe_function_matches(row,function_name)
        ]
        allocation_candidates=[
            row for row in function_rows
            if 'with_capacity' in str(row.get('callee') or '')
            and 'Vec<ProducerPayload' in ' '.join(
                str(row.get(field) or '')
                for field in ('semantic_object_type','destination_type')
            )
        ]
        target_drop_or_deallocation_rows=[]
        for row in function_rows:
            operation_text=' '.join(
                str(row.get(field) or '')
                for field in (
                    'lowering_kind',
                    'rewrite_status',
                    'callee',
                    'replacement_symbol',
                    'metadata_pairing_contract',
                )
            ).lower()
            if 'drop' in operation_text or 'dealloc' in operation_text:
                target_drop_or_deallocation_rows.append(row)
        assert len(allocation_candidates)==1, f"{function_name} must have exactly one supported Vec allocation scope candidate, got {len(allocation_candidates)}"
        scope_row=allocation_candidates[0]
        actual_type_isolated_scope(scope_row, f"{function_name} supported Vec allocation scope")
        assert not target_drop_or_deallocation_rows, f"{function_name} must not have target Drop/deallocation scope evidence; allocation-side recovery is the required mechanism: {target_drop_or_deallocation_rows!r}"
        all_rows.append(scope_row)
        evidence[function_name]={
            'allocation_scope_rows':len(allocation_candidates),
            'target_drop_or_deallocation_rows':len(target_drop_or_deallocation_rows),
            'allocation_side_recovery_required':True,
        }
    type_ids={int(row.get('type_id') or 0) for row in all_rows}
    assert len(type_ids)==1, f"supported seed/recovery must share one compiler type_id, got {sorted(type_ids)}"
    type_id=next(iter(type_ids)); assert type_id!=0, "supported compiler type_id must be nonzero"
    module_ids={int(row.get('module_id') or 0) for row in all_rows}
    assert len(module_ids)==1, f"supported seed/recovery must share one module_id, got {sorted(module_ids)}"
    module_id=next(iter(module_ids)); assert module_id!=0, "supported compiler module_id must be nonzero"
    return {'type_id':type_id,'module_id':module_id,'allocation_side_recovery_required':True,'functions':evidence}

def validate_audit(audit: Dict[str,Any]) -> Dict[str,Any]:
    s=audit.get('summary') or {}; assert s.get('provider_override_installed') is True; assert s.get('body_clone_returned_to_rustc') is True; assert s.get('actual_semantic_scope_rewrite') is True; assert int(s.get('semantic_scope_unsolved_candidate_count') or 0)>=1
    rows=ambiguous_clone_rows(audit); assert rows, f"missing audit row for {AMBIGUOUS_FUNCTION}"
    applied=[r for r in rows if r.get('rewrite_status') in APPLIED_STATUSES]; assert not applied, f"ambiguous Clone must not receive an applied/planned scope: {applied!r}"
    assert len(rows)==1, f"expected exactly one ambiguous Clone audit row, got {len(rows)}: {rows!r}"
    amb=[r for r in rows if r.get('lowering_kind')=='semantic_scope_unsolved_heap_object_candidate' and r.get('rewrite_status')==AMBIGUOUS_STATUS and r.get('replacement_resolution_status')=='rustc_middle_multiple_heap_object_types_not_lowered']
    assert len(amb)==1, f"expected one ambiguous fail-closed row, got {len(amb)}: {rows!r}"
    row=amb[0]; assert 'Result' in str(row.get('destination_type'))
    preview=str(row.get('replacement_preview') or ''); assert 'std::vec::Vec' in preview and 'std::string::String' in preview, preview
    supported=validate_supported_controls(audit)
    plain_clone=validate_plain_clone_control(audit)
    plain_result_clone=validate_plain_result_clone_control(audit)
    wrong_type=validate_wrong_type_control(audit)
    producer_type_id=int(supported['type_id']); producer_module_id=int(supported['module_id'])
    assert int(plain_clone['type_id'])==producer_type_id, f"Option Clone and Producer controls must share one compiler type_id: {plain_clone['type_id']} != {producer_type_id}"
    assert int(plain_clone['module_id'])==producer_module_id, f"Option Clone and Producer controls must share one module_id: {plain_clone['module_id']} != {producer_module_id}"
    assert int(plain_result_clone['type_id'])==producer_type_id, f"Result Clone and Producer controls must share one compiler type_id: {plain_result_clone['type_id']} != {producer_type_id}"
    assert int(plain_result_clone['module_id'])==producer_module_id, f"Result Clone and Producer controls must share one module_id: {plain_result_clone['module_id']} != {producer_module_id}"
    assert int(wrong_type['type_id'])!=producer_type_id, "same-layout Consumer and Producer must have distinct compiler type_id values"
    assert int(wrong_type['module_id'])==producer_module_id, f"same-module Consumer and Producer controls must share one module_id: {wrong_type['module_id']} != {producer_module_id}"
    return {'ambiguous_fail_closed_rows':len(amb),'ambiguous_destination_type':row.get('destination_type'),'ambiguous_preview':preview,'supported_controls':supported,'plain_clone_control':plain_clone,'plain_result_clone_control':plain_result_clone,'wrong_type_control':wrong_type}

def validate_runtime(runtime: Dict[str,Any], compiler_type_id: Optional[int]=None, wrong_type_id: Optional[int]=None) -> Dict[str,Any]:
    assert runtime.get('result_variant')=='Ok'; assert runtime.get('clone_function')==AMBIGUOUS_FUNCTION; assert runtime.get('buffers_distinct') is True; assert int(runtime.get('same_layout_bytes') or 0)==64; assert int(runtime.get('source_len') or 0)==int(runtime.get('cloned_len') or 0)==4
    assert runtime.get('plain_clone_function')==PLAIN_CLONE_FUNCTION; assert runtime.get('plain_clone_variant')=='Some'; assert int(runtime.get('plain_cloned_len') or 0)==4
    source=int(runtime.get('source_buffer') or 0); fallback=int(runtime.get('cloned_buffer') or 0); protected=int(runtime.get('protected_buffer') or 0); recovered=int(runtime.get('recovered_buffer') or 0); plain_source=int(runtime.get('plain_source_buffer') or 0); plain_clone=int(runtime.get('plain_cloned_buffer') or 0); wrong_buffer=int(runtime.get('wrong_type_buffer') or 0)
    assert source!=0 and fallback!=0 and protected!=0 and recovered!=0 and plain_source!=0 and plain_clone!=0 and wrong_buffer!=0
    assert source!=fallback; assert source!=protected, "protected seed reused the still-live source allocation"
    assert runtime.get('fallback_avoided_protected_buffer') is True; assert fallback!=protected, "ambiguous fallback reused the protected typed cache entry"
    assert runtime.get('typed_recovery_preserved') is True; assert recovered==protected, "supported typed recovery did not return the protected cache entry"
    assert runtime.get('plain_clone_buffers_distinct') is True; assert plain_source!=plain_clone, "supported Option Clone aliased its source buffer"
    assert runtime.get('plain_clone_avoided_wrong_type_buffer') is True; assert plain_clone!=wrong_buffer and protected!=wrong_buffer, "supported Option Clone reused same-layout Consumer storage"
    assert runtime.get('plain_clone_reused_protected_buffer') is True; assert plain_clone==protected, "supported Option Clone did not recover the protected Producer cache entry"
    plain_result=runtime.get('plain_result_clone'); assert isinstance(plain_result,dict), "missing supported Result Clone runtime evidence"
    assert plain_result.get('clone_function')==PLAIN_RESULT_CLONE_FUNCTION; assert plain_result.get('variant')=='Ok'; assert int(plain_result.get('cloned_len') or 0)==4
    plain_result_source=int(plain_result.get('source_buffer') or 0); plain_result_clone=int(plain_result.get('cloned_buffer') or 0)
    assert plain_result_source!=0 and plain_result_clone!=0
    assert plain_result.get('buffers_distinct') is True; assert plain_result_source!=plain_result_clone, "supported Result Clone aliased its source buffer"
    assert plain_result.get('avoided_wrong_type_buffer') is True; assert plain_result_clone!=wrong_buffer, "supported Result Clone reused same-layout Consumer storage"
    assert plain_result.get('reused_protected_buffer') is True; assert plain_result_clone==protected, "supported Result Clone did not recover the protected Producer cache entry"
    seed_type_id=int(runtime.get('seed_type_id') or 0); recovery_type_id=int(runtime.get('recovery_type_id') or 0); plain_type_id=int(runtime.get('plain_clone_type_id') or 0); runtime_wrong_type_id=int(runtime.get('wrong_type_id') or 0)
    assert seed_type_id!=0 and recovery_type_id==seed_type_id
    assert plain_type_id==seed_type_id, "supported Option Clone runtime type_id differs from Producer controls"
    assert int(plain_result.get('type_id') or 0)==seed_type_id, "supported Result Clone runtime type_id differs from Producer controls"
    assert runtime_wrong_type_id!=0 and runtime_wrong_type_id!=seed_type_id, "same-layout Consumer runtime type_id must differ from Producer"
    if compiler_type_id is not None: assert seed_type_id==compiler_type_id, f"runtime type_id {seed_type_id} does not match compiler type_id {compiler_type_id}"
    if wrong_type_id is not None: assert runtime_wrong_type_id==wrong_type_id, f"runtime Consumer type_id {runtime_wrong_type_id} does not match compiler type_id {wrong_type_id}"
    assert int(runtime.get('seed_typed_allocations') or 0)==1; assert int(runtime.get('seed_typed_deallocations') or 0)==1; assert int(runtime.get('seed_typed_cache_inserts') or 0)==1
    assert int(runtime.get('wrong_type_typed_allocations') or 0)==1; assert int(runtime.get('wrong_type_typed_deallocations') or 0)==1; assert int(runtime.get('wrong_type_typed_cache_inserts') or 0)==1
    for field in ('wrong_type_fallback_allocations','wrong_type_fallback_deallocations','wrong_type_raw_alloc_no_metadata','wrong_type_raw_dealloc_no_metadata'):
        assert int(runtime.get(field) or 0)==0, f"same-layout Consumer control unexpectedly used fallback path: {field}={runtime.get(field)!r}"
    assert int(runtime.get('plain_clone_typed_allocations') or 0)==1; assert int(runtime.get('plain_clone_typed_deallocations') or 0)==1; assert int(runtime.get('plain_clone_typed_cache_hits') or 0)==1; assert int(runtime.get('plain_clone_typed_cache_inserts') or 0)==1
    for field in ('plain_clone_fallback_allocations','plain_clone_fallback_deallocations','plain_clone_raw_alloc_no_metadata','plain_clone_raw_dealloc_no_metadata','plain_clone_raw_realloc_no_metadata'):
        assert int(runtime.get(field) or 0)==0, f"supported Option Clone unexpectedly used fallback path: {field}={runtime.get(field)!r}"
    for field in ('typed_allocations','typed_deallocations','typed_cache_hits','typed_cache_inserts'):
        assert int(plain_result.get(field) or 0)==1, f"supported Result Clone expected one {field}: {plain_result!r}"
    for field in ('fallback_allocations','fallback_deallocations','raw_alloc_no_metadata','raw_dealloc_no_metadata','raw_realloc_no_metadata'):
        assert int(plain_result.get(field) or 0)==0, f"supported Result Clone unexpectedly used fallback path: {field}={plain_result.get(field)!r}"
    assert int(runtime.get('clone_typed_allocations') or 0)==0; assert int(runtime.get('clone_typed_deallocations') or 0)==0; assert int(runtime.get('clone_fallback_allocations') or 0)==1; assert int(runtime.get('clone_fallback_deallocations_before_drop') or 0)==0; assert int(runtime.get('clone_raw_alloc_no_metadata') or 0)==1; assert int(runtime.get('clone_raw_alloc_no_metadata_bytes') or 0)==256; assert int(runtime.get('clone_raw_realloc_no_metadata') or 0)==0; assert int(runtime.get('clone_recorded_old_realloc_fallback') or 0)==0; assert int(runtime.get('drop_fallback_deallocations') or 0)==1; assert int(runtime.get('drop_raw_dealloc_no_metadata') or 0)==1
    assert int(runtime.get('recovery_typed_allocations') or 0)==1; assert int(runtime.get('recovery_typed_deallocations') or 0)==1; assert int(runtime.get('recovery_typed_cache_hits') or 0)==1; assert int(runtime.get('recovery_typed_cache_inserts') or 0)==1
    assert int(runtime.get('recovery_identity_mismatches') or 0)==0; assert int(runtime.get('side_cache_corrupt_slots') or 0)==0
    return {'protected_buffer':protected,'plain_clone_buffer':plain_clone,'plain_result_clone_buffer':plain_result_clone,'wrong_type_buffer':wrong_buffer,'fallback_buffer':fallback,'recovered_buffer':recovered,'plain_clone_reused_protected_buffer':True,'plain_clone_avoided_wrong_type_buffer':True,'plain_result_clone_reused_protected_buffer':True,'plain_result_clone_avoided_wrong_type_buffer':True,'fallback_avoided_protected_buffer':True,'typed_recovery_preserved':True,'compiler_type_id':compiler_type_id,'wrong_compiler_type_id':wrong_type_id,'clone_raw_alloc_no_metadata':1,'drop_raw_dealloc_no_metadata':1}

def validate(audit: Dict[str,Any], runtime: Dict[str,Any]) -> Dict[str,Any]:
    audit_evidence=validate_audit(audit)
    compiler_type_id=int(audit_evidence['supported_controls']['type_id'])
    wrong_type_id=int(audit_evidence['wrong_type_control']['type_id'])
    return {'audit':audit_evidence,'runtime':validate_runtime(runtime,compiler_type_id,wrong_type_id)}

def parse_args() -> argparse.Namespace:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--output-dir',type=Path); p.add_argument('--toolchain'); p.add_argument('--timeout',type=int,default=300); p.add_argument('--fixed-heap',action='store_true'); return p.parse_args()

def main() -> int:
    if not __debug__: raise SystemExit('do not run this assertion-based validator with python -O')
    a=parse_args(); toolchain=a.toolchain or (ROOT/'rust-toolchain').read_text(encoding='utf-8').strip(); out=a.output_dir.resolve() if a.output_dir else default_output_dir(); reject_output_inside_repo(out); out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()): raise SystemExit(f"output directory must be empty: {out}")
    rewrites=out/'rewrites'; logs=out/'logs'; target=out/'cargo-target'; rewrites.mkdir(); logs.mkdir(); target.mkdir()
    rustc=shutil.which('rustc') or 'rustc'; cargo=shutil.which('cargo') or 'cargo'; start=source_binding_snapshot(toolchain,rustc); sysroot=str(start['sysroot']); pass_bin=out/'unialloc-rustc-mir-rewrite-dry-run'
    benv=os.environ.copy(); benv['RUSTC_BOOTSTRAP']='1'; build=run_logged([rustc,f'+{toolchain}',*current_rustc_cfg(toolchain),str(PASS_SOURCE),'-o',str(pass_bin)],label='pass-build',output_dir=out,timeout=a.timeout,env=benv)
    if build['returncode']!=0: raise SystemExit(f"pass build failed; see {build['stderr']}")
    renv=os.environ.copy()
    for v in ('DYLD_LIBRARY_PATH','LD_LIBRARY_PATH'):
        cur=renv.get(v); renv[v]=f"{sysroot}/lib"+(os.pathsep+cur if cur else '')
    renv.update({'RUSTC_WRAPPER':str(pass_bin),'UNIALLOC_RUSTC_TARGET_CRATES':PROBE_NAME,'UNIALLOC_REWRITE_AUDIT_DIR':str(rewrites),'UNIALLOC_PASS_LOG_DIR':str(logs),'UNIALLOC_CONTINUE_COMPILATION':'1','UNIALLOC_ACTUAL_MIR_REWRITE':'1','UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE':'1','UNIALLOC_LOWERING_POLICY_FLAGS':'1','UNIALLOC_RUSTC_SYSROOT':sysroot,'CARGO_NET_OFFLINE':'true','CARGO_INCREMENTAL':'0','CARGO_TARGET_DIR':str(target)})
    features=['stats','type_isolation']
    if a.fixed_heap: features.append('fixed_heap')
    run=run_logged([cargo,f'+{toolchain}','run','--quiet','-p','unialloc','--bin',PROBE_NAME,'--features',','.join(features)],label='probe-run',output_dir=out,timeout=a.timeout,env=renv)
    if run['returncode']!=0: raise SystemExit(f"probe run failed; see {run['stderr']}")
    paths=sorted(rewrites.glob('*.json'))
    if len(paths)!=1: raise SystemExit(f"expected one target audit, found {len(paths)} in {rewrites}")
    audit=json.loads(paths[0].read_text(encoding='utf-8')); runtime=load_runtime_event(Path(run['stdout'])); validation=validate(audit,runtime); shutil.rmtree(target); end=source_binding_snapshot(toolchain,rustc); assert_source_binding_stable(start,end)
    summary={'schema_version':1,'source':'mir_ambiguous_clone_fallback_probe_summary','validated':True,'fixed_heap':a.fixed_heap,'toolchain':toolchain,'rustc':start['rustc_verbose_version'],'sysroot':sysroot,'git_head':start['git_head'],'git_status':start['git_status'],'source_binding':{'start':start,'end':end,'drift_checked':True,'commit_bound':True},'features':features,'build':build,'run':run,'artifacts':{'pass_source':str(PASS_SOURCE),'pass_source_sha256':sha256(PASS_SOURCE),'pass_binary':str(pass_bin),'pass_binary_sha256':sha256(pass_bin),'probe_source':str(PROBE_SOURCE),'probe_source_sha256':sha256(PROBE_SOURCE),'rewrite_audit':str(paths[0]),'rewrite_audit_sha256':sha256(paths[0])},'validation':validation,'runtime':runtime,'boundaries':['Functional compiler-pass and runtime type-isolation regression only; no benchmark or paper-performance claim.','The Rust source uses ordinary Vec, Option::clone, and Result::clone operations and no manual metadata allocator ABI calls.','The pass must actually lower the single-owner Option<Vec<ProducerPayload>> and Result<Vec<ProducerPayload>, u8> Clone calls plus supported Vec controls to one Producer identity while assigning same-layout ConsumerPayload a distinct identity.','Runtime typed allocation/deallocation counters and exact address relations prove both supported wrapper Clones recovered Producer storage, did not reuse Consumer storage, and used no fallback path.','The ambiguous Result<Vec<ProducerPayload>, String> Clone remains exactly one fail-closed compiler row and one raw runtime allocation/deallocation; no broader Clone coverage claim is made.']}
    sp=out/'summary.json'; sp.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n',encoding='utf-8'); print(json.dumps({'summary':str(sp),'validated':True},sort_keys=True)); return 0
if __name__=='__main__': sys.exit(main())
