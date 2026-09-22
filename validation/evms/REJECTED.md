# Rejected / deferred EVM candidates

| Topology | Board / source | Reason |
|---|---|---|
| boost | TI SNVA385 (fetched) | Wrong document: it is the LM3445 LED-driver EVM guide, not the LM5122 boost EVM (search-result literature number was wrong). |
| boost | TI SNVA729A (LM5122EVM-1PH, fetched) | Current rev uses the LMG5200 GaN power stage (integrated half-bridge), not discrete MOSFETs — the rig's injected MOSFET schema (Rds_on/Qg/Qgd/plateau per discrete part) has no defensible mapping. |
| boost | TI LM5122EVM-2PH (SNVA815) | Dual-phase — the pipeline validates single-phase only. |
| buck_boost | TI SLUUBC4 / SLUUBC6 (LM5175EVM user's guides) | URLs 404 at TI's literature server; SNVU439 (LM5175EVM-HD) fetched instead as the selected candidate. |
| buck_boost | TI SNVU439 (LM5175EVM-HD) — **SELECTED, IN PROGRESS** | 4-switch non-inverting (exact topology match), 12 V / 6 A @ 400 kHz, full BOM (QH1/QL1 = BSZ042N06NS, QH2/QL2 = BSZ0902NS, L1 = Wurth 7443551370 3.7 uH/4.9 mOhm, 6x 10 uF 50 V output ceramics), "Efficiency vs. Output Current" figure present and vector-digitizable. BLOCKED ON: verified per-part MOSFET fields for BSZ042N06NS / BSZ0902NS (searches returned only family-typical approximations, which the plan forbids); next action = extract the Infineon datasheet PDFs programmatically as done for the TI catalog, then digitize fig 5 and run the sweep. Sim-model caveat to record: the pipeline drives all four switches with ONE MOSFET part; the EVM uses BSZ042 on one leg and BSZ0902 on the other — validation point VIN = 12 V (transition, all four conduct equally) minimizes the mismatch, which will be stated as a caveat. |
| buck_boost | ADI LTC3780 DC1163 | Quick-start guide (4 pages) lacks stated thermal conditions and a digitizable measured-curve source in the fetched form; deferred as the backup candidate. |
