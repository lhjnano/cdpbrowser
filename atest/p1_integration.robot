*** Settings ***
Documentation     P1 integration — accessibility locators + iframe + dialogs + downloads
...               in one flow. Walks fixtures/a11y.html, frames.html, alert.html,
...               downloads.html to cross-verify the whole P1 stack.
Library           CdpBrowser
Library           OperatingSystem

*** Variables ***
${FIX}       ${CURDIR}${/}fixtures

*** Test Cases ***
Accessibility Locators Drive Interactions
    [Documentation]    role:/label: strategies working with interaction keywords.
    ${url}=    Evaluate    __import__('pathlib').Path(r'${FIX}${/}a11y.html').resolve().as_uri()
    Go To    ${url}
    ${save}=    Get Attribute    role:button Save    id
    Should Be Equal As Strings    ${save}    btn-save
    Fill Text    label:Email    user@example.com
    ${count}=    Get Element Count    role:button
    Should Be True    ${count} >= 3

Frame Scoping Isolates And Restores
    [Documentation]    Enter iframe -> operate inside -> Reset back.
    ${url}=    Evaluate    __import__('pathlib').Path(r'${FIX}${/}frames.html').resolve().as_uri()
    Go To    ${url}
    Switch To Frame    ${0}
    Element Should Be Visible    testid:child-name
    Reset Frame    ALL
    Element Should Be Visible    testid:main-title

Dialog And Download Complete The P1 Stack
    [Documentation]    Arm-pattern dialog and download in the same test.
    ${alert_url}=    Evaluate    __import__('pathlib').Path(r'${FIX}${/}alert.html').resolve().as_uri()
    Go To    ${alert_url}
    ${text}=    Handle Alert    ACCEPT    Click    testid:alert-button
    Should Be Equal As Strings    ${text}    Passwords don't match
    ${dl_url}=    Evaluate    __import__('pathlib').Path(r'${FIX}${/}downloads.html').resolve().as_uri()
    Go To    ${dl_url}
    ${promise}=    Promise Next Download
    Click    testid:blob-download
    ${file}=    Wait For    ${promise}
    File Should Exist    ${file}
    ${content}=    Get File    ${file}
    Should Contain    ${content}    quarterly report body
    Numbered Screenshot    p1-integration-complete
