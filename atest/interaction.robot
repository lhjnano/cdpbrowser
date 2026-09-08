*** Settings ***
Documentation     CdpBrowser interaction acceptance tests — poll-by-default keywords,
...               negative-polling assertions, timeout gating, and evidence capture on real Chrome.
Library           CdpBrowser
Library           OperatingSystem
Suite Teardown    Close Browser

*** Variables ***
${FIXTURE}        file://${CURDIR}/fixtures/demo.html

*** Test Cases ***
Click Changes Text And Get Text Reads The Change
    Go To    ${FIXTURE}
    ${before}=    Get Text    testid:swap-button
    Should Be Equal    ${before}    before click
    Click    testid:swap-button
    ${after}=    Get Text    testid:swap-button
    Should Be Equal    ${after}    after click

Fill Text Reflects Value Through Input Event
    Go To    ${FIXTURE}
    Fill Text    testid:name-input    Jane Doe
    ${mirror}=    Get Text    testid:name-mirror
    Should Be Equal    ${mirror}    Jane Doe

Element Should Be Visible And Exist Pass On Present Elements
    Go To    ${FIXTURE}
    Element Should Be Visible    testid:page-title
    Element Should Exist    testid:name-input

Element Should Not Exist Waits For Js Removal
    Go To    ${FIXTURE}
    Element Should Exist    testid:doomed-element
    Click    testid:vanish-button
    Element Should Not Exist    testid:doomed-element

Click On Missing Element Times Out Per Set Timeout
    Go To    ${FIXTURE}
    Set Timeout    1s
    Run Keyword And Expect Error    *Click 'testid:ghost-button'*within 1s*not-found*
    ...    Click    testid:ghost-button
    [Teardown]    Set Timeout    5s

Numbered Screenshot Creates Evidence File
    Go To    ${FIXTURE}
    ${path}=    Numbered Screenshot    interaction-check
    File Should Exist    ${path}
    File Should Not Be Empty    ${path}
    Should Contain    ${path}    evidence
