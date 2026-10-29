# Besu QBFT experiment summary

## Replay of the framework's ledger traces (besu/v26.9.0/linux-x86_64/openjdk-java-25)

| Trace | Days | Reports | Transactions | Blocks | Equivalent to reference | Mean time per day (s) | Gas per day |
|---|---|---|---|---|---|---|---|
| ρ = 0.00, seed 44 | 40 | 41 | 220 | 189 | yes, every day | 9.3 | 738,984 |
| ρ = 0.25, seed 44 | 40 | 49 | 236 | 191 | yes, every day | 9.4 | 807,415 |
| ρ = 0.50, seed 44 | 40 | 68 | 259 | 196 | yes, every day | 9.6 | 965,097 |
| ρ = 0.75, seed 44 | 40 | 70 | 271 | 202 | yes, every day | 9.9 | 980,196 |
| ρ = 1.00, seed 44 | 40 | 70 | 271 | 202 | yes, every day | 9.9 | 980,196 |

| Operation | Transactions | Mean gas | Latency p50 (ms) | Latency p95 (ms) |
|---|---|---|---|---|
| registerMembers | 5 | 354,996 | 2133 | 2133 |
| setOwners | 5 | 276,826 | 2154 | 2154 |
| commitPlan | 200 | 70,998 | 1930 | 2181 |
| postOutcome | 200 | 214,836 | 2126 | 2190 |
| scoreDay | 200 | 185,942 | 1950 | 2185 |
| submitReport | 298 | 122,896 | 1918 | 2207 |
| postClaims | 167 | 154,514 | 2127 | 2177 |
| postSensorRecords | 182 | 104,036 | 1919 | 2220 |

## Scaling benchmark (besu/v26.9.0/linux-x86_64/openjdk-java-25)

| Stakeholders | Reports per day | Blocks to include the burst | Report latency p50 / p95 (ms) | Scoring gas per report | Scoring transactions per day | Blocks per day |
|---|---|---|---|---|---|---|
| 10 | 3 | 1.0 | 2058 / 2060 | 131,766 | 1.0 | 6.0 |
| 100 | 30 | 1.0 | 1953 / 2022 | 121,902 | 1.0 | 6.0 |
| 1000 | 300 | 2.7 | 4133 / 5107 | 116,626 | 3.0 | 14.3 |

## Fault tolerance

| Test | Probes sent | Included | Max latency (s) | Longest gap between blocks (s) |
|---|---|---|---|---|
| one_down | 80 | 80 | 6.1 | 6.0 |
| two_down | 80 | 80 | 229.2 | 3.0 |
