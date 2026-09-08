*** Settings ***
Documentation     File download — arm -> trigger -> Wait For (path) pattern.
Library           CdpBrowser
Library           OperatingSystem

*** Test Cases ***
Downloaded File Lands With Suggested Name And Content
    ${url}=    Evaluate    __import__('pathlib').Path(r'${CURDIR}/fixtures/downloads.html').resolve().as_uri()
    Go To    ${url}
    ${promise}=    Promise Next Download
    Click    testid:blob-download
    ${file}=    Wait For    ${promise}
    File Should Exist    ${file}
    ${name}=    Evaluate    __import__('os').path.basename(r'${file}')
    Should Be Equal As Strings    ${name}    report.txt
    ${content}=    Get File    ${file}
    Should Contain    ${content}    quarterly report body
    Numbered Screenshot    download-verified

Second Download Gets Unique Name
    ${url}=    Evaluate    __import__('pathlib').Path(r'${CURDIR}/fixtures/downloads.html').resolve().as_uri()
    Go To    ${url}
    ${p1}=    Promise Next Download
    Click    testid:blob-download
    ${f1}=    Wait For    ${p1}
    ${p2}=    Promise Next Download
    Click    testid:blob-download-2
    ${f2}=    Wait For    ${p2}
    File Should Exist    ${f1}
    File Should Exist    ${f2}
    Should Not Be Equal As Strings    ${f1}    ${f2}
