#!/usr/bin/env python3
"""Run and validate the fail-closed ambiguous Clone fallback probe."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[2]
RUNNER_SOURCE = Path(__file__).resolve()
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "rustc_driver_mir_ambiguous_clone_fallback_probe"
PROBE_SOURCE = ROOT / "unialloc/src/bin" / f"{PROBE_NAME}.rs"
SOURCE_BINDING_PATHS = (Path("Cargo.toml"), Path("Cargo.lock"), Path("rust-toolchain"), Path("alloc_macros/Cargo.toml"), Path("alloc_macros/src"), Path("unialloc/Cargo.toml"), Path("unialloc/build.rs"), Path("unialloc/src"), PASS_SOURCE.relative_to(ROOT), RUNNER_SOURCE.relative_to(ROOT))
AMBIGUOUS_FUNCTION = "Result::clone"
AMBIGUOUS_STATUS = "semantic_scope_rewrite_skipped_ambiguous_heap_object_type"
APPLIED_STATUSES = {"actual_semantic_scope_enter_exit_rewrite_applied", "semantic_scope_enter_exit_rewrite_planned", "actual_semantic_scope_drop_rewrite_applied", "semantic_scope_drop_rewrite_planned"}

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

def validate_audit(audit: Dict[str,Any]) -> Dict[str,Any]:
    s=audit.get('summary') or {}; assert s.get('provider_override_installed') is True; assert s.get('body_clone_returned_to_rustc') is True; assert s.get('actual_semantic_scope_rewrite') is True; assert int(s.get('semantic_scope_unsolved_candidate_count') or 0)>=1
    rows=ambiguous_clone_rows(audit); assert rows, f"missing audit row for {AMBIGUOUS_FUNCTION}"
    amb=[r for r in rows if r.get('lowering_kind')=='semantic_scope_unsolved_heap_object_candidate' and r.get('rewrite_status')==AMBIGUOUS_STATUS and r.get('replacement_resolution_status')=='rustc_middle_multiple_heap_object_types_not_lowered']
    assert len(amb)==1, f"expected one ambiguous fail-closed row, got {len(amb)}: {rows!r}"
    row=amb[0]; assert 'Result' in str(row.get('destination_type'))
    preview=str(row.get('replacement_preview') or ''); assert 'std::vec::Vec' in preview and 'std::string::String' in preview, preview
    applied=[r for r in rows if r.get('rewrite_status') in APPLIED_STATUSES]; assert not applied, f"ambiguous Clone must not receive an applied/planned scope: {applied!r}"
    return {'ambiguous_fail_closed_rows':len(amb),'ambiguous_destination_type':row.get('destination_type'),'ambiguous_preview':preview}

def validate_runtime(runtime: Dict[str,Any]) -> Dict[str,Any]:
    assert runtime.get('result_variant')=='Ok'; assert runtime.get('clone_function')==AMBIGUOUS_FUNCTION; assert runtime.get('buffers_distinct') is True; assert int(runtime.get('same_layout_bytes') or 0)==64; assert int(runtime.get('source_len') or 0)==int(runtime.get('cloned_len') or 0)==4
    assert int(runtime.get('source_buffer') or 0)!=0 and int(runtime.get('cloned_buffer') or 0)!=0 and int(runtime.get('source_buffer') or 0)!=int(runtime.get('cloned_buffer') or 0)
    assert int(runtime.get('clone_typed_allocations') or 0)==0; assert int(runtime.get('clone_fallback_allocations') or 0)>=1; assert int(runtime.get('clone_raw_alloc_no_metadata') or 0)>=1; assert int(runtime.get('clone_raw_alloc_no_metadata_bytes') or 0)>=256; assert int(runtime.get('clone_raw_realloc_no_metadata') or 0)==0; assert int(runtime.get('clone_recorded_old_realloc_fallback') or 0)==0; assert int(runtime.get('drop_fallback_deallocations') or 0)>=1; assert int(runtime.get('drop_raw_dealloc_no_metadata') or 0)>=1; assert int(runtime.get('recovery_identity_mismatches') or 0)==0; assert int(runtime.get('side_cache_corrupt_slots') or 0)==0
    return {'clone_fallback_allocations':int(runtime.get('clone_fallback_allocations') or 0),'clone_raw_alloc_no_metadata':int(runtime.get('clone_raw_alloc_no_metadata') or 0),'drop_raw_dealloc_no_metadata':int(runtime.get('drop_raw_dealloc_no_metadata') or 0)}

def validate(audit: Dict[str,Any], runtime: Dict[str,Any]) -> Dict[str,Any]: return {'audit':validate_audit(audit),'runtime':validate_runtime(runtime)}

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
    summary={'schema_version':1,'source':'mir_ambiguous_clone_fallback_probe_summary','validated':True,'fixed_heap':a.fixed_heap,'toolchain':toolchain,'rustc':start['rustc_verbose_version'],'sysroot':sysroot,'git_head':start['git_head'],'git_status':start['git_status'],'source_binding':{'start':start,'end':end,'drift_checked':True,'commit_bound':True},'features':features,'build':build,'run':run,'artifacts':{'pass_source':str(PASS_SOURCE),'pass_source_sha256':sha256(PASS_SOURCE),'pass_binary':str(pass_bin),'pass_binary_sha256':sha256(pass_bin),'probe_source':str(PROBE_SOURCE),'probe_source_sha256':sha256(PROBE_SOURCE),'rewrite_audit':str(paths[0]),'rewrite_audit_sha256':sha256(paths[0])},'validation':validation,'runtime':runtime,'boundaries':['Functional compiler-pass fail-closed regression only; no benchmark or paper-performance claim.','The Rust source uses ordinary Result::clone and no manual metadata allocator ABI calls.','The pass must audit the ambiguous Vec/String Clone result and skip semantic-scope lowering for that call.','Runtime fallback counters prove the cloned Vec buffer used conventional raw fallback while output contents remained correct.']}
    sp=out/'summary.json'; sp.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n',encoding='utf-8'); print(json.dumps({'summary':str(sp),'validated':True},sort_keys=True)); return 0
if __name__=='__main__': sys.exit(main())
