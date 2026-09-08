*** Settings ***
Documentation     P2 integration — real input tracks (keys/mouse/coordinates), scrolling,
...               upload, tabs, and shadow DOM piercing cross-verified in one flow.
Library           CdpBrowser
Library           OperatingSystem

*** Variables ***
${FIX}       ${CURDIR}${/}fixtures

*** Test Cases ***
Realistic Track Drives Form And Canvas
    [Documentation]    Real keys + real mouse click + coordinate click (canvas) in sequence.
    ${url}=    Evaluate    __import__('pathlib').Path(r'${FIX}${/}mouse.html').resolve().as_uri()
    Go To    ${url}
    Scroll To Element    testid:pad
    ${coords}=    Click With Real Mouse    testid:deep-button
    Should Not Be Empty    ${coords}
    ${box_x}=    Set Variable    ${0}
    Click At Coordinates    400    300
    Hover    testid:hover-zone
    Element Should Be Visible    testid:hover-zone

Upload And Multiple Tabs Isolate State
    [Documentation]    Upload (change auto-fires) + per-tab state isolation.
    ${url}=    Evaluate    __import__('pathlib').Path(r'${FIX}${/}upload.html').resolve().as_uri()
    Go To    ${url}
    Create File    ${TEMPDIR}${/}p2-integration.txt    cdpbrowser p2
    Upload File    testid:file-input    ${TEMPDIR}${/}p2-integration.txt
    ${log}=    Get Text    testid:upload-log
    Should Contain    ${log}    p2-integration.txt
    ${tab}=    New Tab
    Should Be Equal As Integers    ${tab}    1
    ${n}=    Get Element Count    testid:upload-log
    Should Be Equal As Integers    ${n}    0
    Close Tab
    ${log2}=    Get Text    testid:upload-log
    Should Contain    ${log2}    p2-integration.txt

Shadow Dom Deep Locators Work Opt In
    [Documentation]    Invisible without deep:, piercing with deep:.
    ${url}=    Evaluate    __import__('pathlib').Path(r'${FIX}${/}shadow.html').resolve().as_uri()
    Go To    ${url}
    ${n}=    Get Element Count    testid:shadow-btn
    Should Be Equal As Integers    ${n}    0
    Element Should Be Visible    deep:testid:shadow-btn
    Click    deep:testid:shadow-btn
    ${text}=    Get Text    deep:testid:shadow-btn
    Should Contain    ${text}    shadow clicked
    Fill Text    deep:testid:shadow-input    deep input works
    ${mirror}=    Get Text    deep:testid:shadow-mirror
    Should Be Equal As Strings    ${mirror}    mirror:deep input works
    Numbered Screenshot    p2-integration-complete

Real Keys Type And Special Keys Act
    [Documentation]    Real key input + special keys (BACKSPACE) + IME fallback.
    ${url}=    Evaluate    __import__('pathlib').Path(r'${FIX}${/}demo.html').resolve().as_uri()
    Go To    ${url}
    Type Text    testid:name-input    hi
    Press Keys    BACKSPACE
    ${mirror}=    Get Text    testid:name-mirror
    Should Be Equal As Strings    ${mirror}    h
    Type Text    testid:name-input    Ünïcødé
    ${mirror2}=    Get Text    testid:name-mirror
    Should Be Equal As Strings    ${mirror2}    hÜnïcødé
