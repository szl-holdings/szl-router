# Forge Dual-GPU Mesh - sovereign, measured, set in stone

Two GPUs in one laptop run as two independent inference workers behind a tiny
least-connections router. Sovereign own-metal. Every number below is **MEASURED**
on the machine (2026-06-14), not estimated.

## Topology
- **dGPU** - NVIDIA RTX 5050 (Blackwell, CUDA), Ollama on `0.0.0.0:11434` - heavy chat/generate
- **iGPU** - Intel Arc 140T (Vulkan), Ollama on `0.0.0.0:11435` - embeddings + overflow
- **Router** - `forge-mesh-router.py` (Python stdlib, zero deps) on `0.0.0.0:11500`
  - embeddings -> iGPU
  - chat/generate -> least-connections, prefer dGPU
  - automatic failover; every response stamped `X-Forge-Backend`
  - `GET /mesh/status` for liveness

> Lesson learned: pooling both GPUs into ONE model is *slower* (~6 tok/s) than
> running two specialised workers. Two specialists beat one confused giant.

## Measured performance (qwen2.5-coder:7b)

| Metric | dGPU (RTX 5050) | iGPU (Arc 140T) |
|---|---|---|
| Throughput | 37.8 tok/s | 15.2 tok/s |
| Energy / token | 0.619 J | not isolatable via NVML* |
| Active power (mean / peak) | 23.4 W / 38.8 W | - |
| Idle power | 9.84 W | - |
| 400-token answer | 247.6 J in 10.6 s | - |

\* Intel iGPU power is not exposed through nvidia-smi, so we only claim MEASURED
power for the NVIDIA card - we never fabricate the Intel figure. A wall-plug meter
would measure whole-laptop draw honestly.

**Method:** live NVML power sampling (`nvidia_smi_power_draw_watts`) at ~5 Hz during
a real 400-token generation; J = mean_active_W x eval_duration.

## Set in stone (survives reboot, no login required)
- `ForgeRestoreOllama` - dGPU worker - Windows scheduled task, **AtStartup**
- `ForgeOllamaIGPU` - iGPU worker - **AtStartup**
- `ForgeNVMLExporter` - power meter on `:9835` - **AtStartup**, highest privileges
- `forge-mesh-router.service` - systemd, **enabled + active**
- Reversible: pre-mesh env backed up; one-line rollback.

## Use it
Point any OpenAI/Ollama-compatible client at `http://<gpu-host>:11500`.
Health: `GET /mesh/status`. Live watts: `GET http://<gpu-host>:9835/metrics`.

## Economics (honest)
Renting this 8 GB laptop GPU on Vast.ai is roughly $0-10/mo gross and ~$5-11/mo in
power: break-even to a small loss, and it ties up the machine. The real ROI is the
API spend you avoid by running inference yourself. Keep it sovereign.

See `examples/forge-mesh-router.py` for the reference router.
