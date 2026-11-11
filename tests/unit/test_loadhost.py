from orchestrate.loadhost import max_cpu_percent, remote_script, run_is_valid

VMSTAT = """procs -----------memory---------- ---swap-- -----io---- -system-- -------cpu-------
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st gu
 1  0      0 7000000  2000 400000    0    0   120    40  300  500  9  3 87  1  0  0
 3  0      0 6900000  2000 401000    0    0     0     8 4100 9000 31 12 57  0  0  0
 2  0      0 6890000  2000 401500    0    0     0     4 3900 8800 28 10 62  0  0  0
"""


def test_cpu_peak_ignores_the_since_boot_line():
    assert max_cpu_percent(VMSTAT) == 43          # 100 - 57; the first line (13%) is skipped


def test_remote_script_quotes_every_k6_variable():
    script = remote_script("C1-Base-x", "s3://b/runs/C1-Base-x/",
                           {"BASE_URL": "https://a.example/", "CONFIG": "Retry+Cache"})
    assert "-e BASE_URL=https://a.example/" in script
    assert "-e CONFIG=Retry+Cache" in script
    assert "aws s3 cp s3://b/runs/C1-Base-x/workload.js" in script


def test_run_validity_rule():
    assert run_is_valid({"dropped_iterations": 0}, 70)
    assert not run_is_valid({"dropped_iterations": 1}, 20)
    assert not run_is_valid({"dropped_iterations": 0}, 71)
