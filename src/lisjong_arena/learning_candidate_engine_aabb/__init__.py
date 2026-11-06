"""Learning family昇格評価（protocol ``arena-learning-candidate-engine-aabb-v1``、#562）。

仕様・運用は ``docs/learning-candidate-engine-aabb.md``。設計の正本はlisjong#368。

```text
protocol      固定値（K = 100、平均最終順位、6配置、rules / engine）、record layout
participants  L2a合成Policy（A）とArm Y（B）のbinding document
runtime       worker内のML条件・3 artifactのload・1半荘実行（新しいPolicy、判断時間）
statistics    R_A / R_B、D、区間、classification、final_points差（副指標）
lock          実行前lock、Seed Registry照合、live gate
result        正式result・観測値（時間 / RSS）・offline verify
limits        資源上限の強制（OS scopeの照合、全体wall、親 + workerのメモリ、#565）
runner        正式eventの実行入口（STOP時の診断証跡）
```

局精算record・副指標・executorは ``heuristic_candidate_engine_aabb`` の部品を再利用し、
Heuristic / Overallの契約・artifact schemaは変更しない。依存はこのpackage →
Heuristic engine AABBの部品の一方向である。
"""
