from scripts.assert_no_skips import main


def test_junit_report_with_no_skips_passes(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="1" skipped="0"><testcase name="ok"/></testsuite>',
        encoding="utf-8",
    )
    assert main([str(report)]) == 0


def test_junit_report_with_skips_fails(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="1" skipped="1"><testcase name="skipped"/></testsuite>',
        encoding="utf-8",
    )
    assert main([str(report)]) == 1


def test_junit_report_with_dtd_or_entities_fails_closed(tmp_path):
    report = tmp_path / "unsafe.xml"
    report.write_text(
        '<!DOCTYPE testsuite [<!ENTITY x "expanded">]>'
        '<testsuite tests="1"><testcase name="&x;"/></testsuite>',
        encoding="utf-8",
    )
    assert main([str(report)]) == 2
