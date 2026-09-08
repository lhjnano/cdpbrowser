*** Settings ***
Documentation     P3 integration — cookies (http origin), viewport/mobile emulation,
...               visual baseline roundtrip with masking, and the ext: registry
...               locator, cross-verified in one flow. Baselines land in ${TEMPDIR}
...               so the repo stays clean.
Library           CdpBrowser    baseline_dir=${TEMPDIR}
Library           OperatingSystem
Library           Process
Suite Setup       Start Fixture Server
Suite Teardown    Run Keywords    Stop Fixture Server    AND    Close Browser

*** Variables ***
${FIX}       ${CURDIR}${/}fixtures
${SERVER}    ${EMPTY}

*** Keywords ***
Start Fixture Server
    [Documentation]    Cookies need a real http origin (file:// is special-cased
    ...    by Chrome), so serve the fixtures with a short-lived http.server.
    ${port}=    Evaluate    __import__('random').randint(20000, 39999)
    Start Process    python3    -m    http.server    ${port}    --bind    127.0.0.1    --directory    ${FIX}    alias=fixtures
    ${origin}=    Set Variable    http://127.0.0.1:${port}
    Set Suite Variable    ${SERVER}    ${origin}
    # Wait until the server answers.
    Wait Until Keyword Succeeds    5s    0.2s    Go To    ${origin}/demo.html

Stop Fixture Server
    [Teardown]    Run Keyword And Ignore Error    Stop Process    fixtures
    Run Keyword And Ignore Error    Wait For Process    fixtures    timeout=2s

*** Test Cases ***
Cookies Survive Navigation On Http Origin
    [Documentation]    Set Cookie -> navigate -> the cookie persists and is
    ...    visible to page JavaScript.
    Delete All Cookies
    Set Cookie    ${SERVER}/    session    p3-integration
    Go To    ${SERVER}/form.html
    ${cookies}=    Get Cookies    ${SERVER}
    Length Should Be    ${cookies}    1
    Should Be Equal    ${cookies}[0][name]    session
    Should Be Equal    ${cookies}[0][value]    p3-integration
    ${visible}=    Run Javascript    document.cookie
    Should Contain    ${visible}    session=p3-integration

Viewport Override Drives Layout And Touch
    [Documentation]    Fixed viewport reflects in layout metrics; the mobile
    ...    flag turns on touch emulation; Reset restores.
    Set Viewport Size    480    800
    ${width}=    Run Javascript    window.innerWidth
    Should Be Equal As Integers    ${width}    480
    Set Viewport Size    375    812    mobile=${TRUE}
    ${touch}=    Run Javascript    navigator.maxTouchPoints
    Should Be True    ${touch} > 0
    Reset Viewport
    ${restored}=    Run Javascript    [window.innerWidth, window.innerHeight]
    Should Not Be Equal As Integers    ${restored}[0]    375

Visual Baseline Roundtrip With Masking
    [Documentation]    Baseline -> identical page passes; a mutation fails with
    ...    a diagnosis; update mode rewrites and passes. The volatile element
    ...    is excluded via mask=.
    Set Viewport Size    800    600
    Go To    ${SERVER}/demo.html
    Run Javascript    window.__cdpb_mask_demo = 0; 'ok'
    ${injected}=    Run Javascript    (function(){var d=document.createElement('div');d.setAttribute('data-testid','volatile');d.style.cssText='position:fixed;top:8px;right:8px;width:120px;background:#fff;padding:4px;font:12px monospace';d.textContent='T='+(++window.__cdpb_mask_demo);document.body.appendChild(d);return 'ok';})()
    Update Baseline    p3-shot    mask=testid:volatile
    Page Should Match Baseline    p3-shot    mask=testid:volatile
    # Mutate the page outside the masked region — must fail with a diagnosis.
    Run Javascript    document.body.style.background = 'rgb(0, 96, 0)'; 'ok'
    Run Keyword And Expect Error    *global-shift*    Page Should Match Baseline    p3-shot    mask=testid:volatile
    # Update mode rewrites the baseline and passes.
    Set Environment Variable    CDPBROWSER_UPDATE_BASELINES    1
    Page Should Match Baseline    p3-shot    mask=testid:volatile
    Remove Environment Variable    CDPBROWSER_UPDATE_BASELINES
    Page Should Match Baseline    p3-shot    mask=testid:volatile
    Reset Viewport

Ext Registry Locator Drives Interaction
    [Documentation]    ComponentQuery-style lookup through the fake-Ext fixture:
    ...    count, attribute read, click — no DOM selectors involved.
    Go To    file://${FIX}${/}ext.html
    ${count}=    Get Element Count    ext:button
    Should Be Equal As Integers    ${count}    3
    ${id}=    Get Attribute    ext:#save    id
    Should Be Equal As Strings    ${id}    btn-save
    Click    ext:grid[itemId=users] button[text=Save]
    Numbered Screenshot    p3-integration-complete
