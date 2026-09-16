# ruff: noqa
import pytest
from narang_rider.adapter_certification import *

def manifest(capabilities=frozenset(AdapterCapability)):
    return AdapterManifest("pos-a","provider","1",capabilities,"vault:auth","vault:callback")

def evidence(**changes):
    values=dict(manifest=manifest(),order_create_passed=True,status_callback_passed=True,
        duplicate_replay_passed=True,conflicting_replay_blocked=True,cancellation_passed=True,
        fee_quote_passed=True,pii_minimization_passed=True)
    values.update(changes); return CertificationEvidence(**values)

def test_complete_adapter_is_certified():
    service=AdapterCertificationService(); result=service.certify(evidence())
    assert result.certified
    service.require("pos-a","1",AdapterCapability.REDISPATCH)

def test_missing_required_capability_fails():
    caps=frozenset({AdapterCapability.ORDER_CREATE,AdapterCapability.STATUS_CALLBACK})
    result=AdapterCertificationService().certify(evidence(manifest=manifest(caps)))
    assert not result.certified and "REQUIRED_CAPABILITY_MISSING" in result.failure_codes

@pytest.mark.parametrize("field",["duplicate_replay_passed","conflicting_replay_blocked","pii_minimization_passed"])
def test_security_failure_disables_all_capabilities(field):
    result=AdapterCertificationService().certify(evidence(**{field:False}))
    assert not result.certified and not result.enabled_capabilities

def test_declared_optional_feature_must_pass():
    result=AdapterCertificationService().certify(evidence(fee_quote_passed=False))
    assert "FEE_QUOTE_FAILED" in result.failure_codes

def test_uncertified_version_cannot_run():
    with pytest.raises(ValueError,match="NOT_CERTIFIED"):
        AdapterCertificationService().require("pos-a","2",AdapterCapability.ORDER_CREATE)
