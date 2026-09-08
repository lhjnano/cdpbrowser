*** Settings ***
Documentation     CdpBrowser library smoke — verifies the lazy auto-launch UX contract.
...               With no import settings, Chrome auto-launches on the first keyword;
...               with no DISPLAY it comes up headless.
Library           CdpBrowser
Suite Teardown    Close Browser

*** Variables ***
${FIXTURE}        file://${CURDIR}/fixtures/demo.html
${TITLE}          CdpBrowser Demo

*** Test Cases ***
Go To Loads Fixture And Get Title Matches
    Go To    ${FIXTURE}
    ${title}=    Get Title
    Should Be Equal    ${title}    ${TITLE}

Get Current Url Contains Fixture Filename
    Go To    ${FIXTURE}
    ${url}=    Get Current Url
    Should Contain    ${url}    demo.html

Open Browser Is A No-op When Already Open
    Go To    ${FIXTURE}
    Open Browser
    ${title}=    Get Title
    Should Be Equal    ${title}    ${TITLE}
