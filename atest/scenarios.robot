*** Settings ***
Documentation     CdpBrowser real-world scenario acceptance tests — reproduces the kopia
...               HTMLUI E2E pattern. data-testid selectors, JS alert state verification
...               (automatic dialog handling is P1 — bypassed via the fixture's alert hook),
...               poll-by-default proven with a 250ms-delayed element, per-step evidence chain.
Library           CdpBrowser
Library           OperatingSystem
Suite Teardown    Close Browser

*** Variables ***
${FIXTURE}        file://${CURDIR}/fixtures/form.html

*** Test Cases ***
Password Mismatch Raises JS Alert Recorded As Page State
    [Documentation]    Mismatched input -> submit -> the form logic really calls alert(),
    ...                and the fixture's hook exposes the message in an inline element.
    ...                Get Text polls until the element appears.
    Go To    ${FIXTURE}
    Fill Text    testid:password    secret123
    Fill Text    testid:confirm-password    secret456
    Click    testid:submit
    ${message}=    Get Text    testid:alert-message
    Should Be Equal    ${message}    Passwords don't match
    Element Should Exist    testid:password

Matching Passwords Reach Delayed Success Screen Via Polling
    [Documentation]    Matching input -> submit -> the success element appears after 250ms.
    ...                Element Should Be Visible polls through the slow element
    ...                and passes (poll-by-default proof).
    Go To    ${FIXTURE}
    Fill Text    testid:password    secret123
    Fill Text    testid:confirm-password    secret123
    Click    testid:submit
    Element Should Be Visible    testid:success
    ${text}=    Get Text    testid:success
    Should Contain    ${text}    Sign-up complete

Evidence Chain Captures Each Step As Numbered Screenshots
    [Documentation]    Leaves evidence with Numbered Screenshot at each step, verifying
    ...                that the per-test-reset NNNN sequence lands in filenames
    ...                (at least 3 shots).
    Go To    ${FIXTURE}
    ${shot1}=    Numbered Screenshot    form-opened
    Fill Text    testid:password    secret123
    ${shot2}=    Numbered Screenshot    password-filled
    Click    testid:submit
    ${shot3}=    Numbered Screenshot    after-submit
    File Should Exist    ${shot1}
    File Should Not Be Empty    ${shot1}
    File Should Exist    ${shot2}
    File Should Not Be Empty    ${shot2}
    File Should Exist    ${shot3}
    File Should Not Be Empty    ${shot3}
    Should Contain    ${shot1}    0001-form-opened.png
    Should Contain    ${shot2}    0002-password-filled.png
    Should Contain    ${shot3}    0003-after-submit.png
