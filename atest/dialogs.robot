*** Settings ***
Documentation     Completes the kopia scenario — verifies a real JS alert() via the arm pattern.
...               (Unlike P0's scenarios.robot scenario 1, which verified via
...               a JS hook bypass, this opens a genuine browser dialog.)
Library           CdpBrowser

*** Test Cases ***
Real Alert Is Captured Via Arm Pattern
    [Documentation]    arm-before-trigger contract (same shape as kopia's expectDialogText):
    ...    Promise Next Alert (arm) -> trigger -> Wait For (harvest).
    ${url}=    Evaluate    __import__('pathlib').Path(r'${CURDIR}/fixtures/alert.html').resolve().as_uri()
    Go To    ${url}
    ${handle}=    Promise Next Alert    action=ACCEPT
    Click    testid:alert-button
    ${text}=    Wait For    ${handle}
    Should Be Equal As Strings    ${text}    Passwords don't match
    Numbered Screenshot    alert-captured

Unarmed Dialog Does Not Deadlock
    [Documentation]    Opening a dialog without an arm lets the backstop auto-dismiss —
    ...    subsequent keywords continue without deadlock.
    ${url}=    Evaluate    __import__('pathlib').Path(r'${CURDIR}/fixtures/alert.html').resolve().as_uri()
    Go To    ${url}
    Click    testid:alert-button
    Element Should Be Visible    testid:title

Convenience Handle Alert Wraps Arm Trigger Wait
    [Documentation]    Handle Alert one-liner — arm -> trigger -> wait + verify.
    ${url}=    Evaluate    __import__('pathlib').Path(r'${CURDIR}/fixtures/alert.html').resolve().as_uri()
    Go To    ${url}
    ${text}=    Handle Alert    ACCEPT    Click    testid:confirm-button
    Should Be Equal As Strings    ${text}    Proceed?
