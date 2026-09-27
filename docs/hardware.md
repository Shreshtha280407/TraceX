# Hardware and offline preflight

Measured on 2026-09-27 using `uname -a`, `lscpu`, `free -h`, `df -h`, `lspci`, and `nvidia-smi` from the development host. Values are a point-in-time inventory, not a performance claim.

| Resource | Observed value | Phase 0 implication |
| --- | --- | --- |
| OS | Arch Linux, kernel `7.2.4-arch1-2`, x86_64 | Linux CPU replay is the primary supported baseline. |
| CPU | Intel Core i5-13420H; 8 physical cores / 12 logical CPUs; max 4.6 GHz | Start later batch/import profiling with 2–4 workers, not all cores by default. |
| Memory | 15 GiB RAM, 15 GiB swap; ~8 GiB available at measurement | This is not the proposed 36 GiB Mac target. Bound batch memory and measure spills. |
| Workspace filesystem | 202 GiB total, ~24 GiB available at measurement | Insufficient headroom for an unbounded 10M-scale run plus duplicate artifacts; preflight free space before any bulk import. |
| GPU | NVIDIA AD107M GeForce RTX 4050 Max-Q visible on PCIe | `nvidia-smi` could not communicate with a driver; CUDA/VRAM is **unavailable and must not be assumed**. |
| Python | 3.14.7 | Phase 0 generator/test checks use only Python standard library. |

## Execution profiles

- **Current Linux CPU profile:** required for Phase 0 validation and future functional replay. No online package installation during a replay; pin/package all dependencies before Phase 6.
- **Mac profile:** referenced in the supplied design as a 36 GB MacBook Pro, but this host is not that machine. Capture chip, core count, free SSD, OS version, and actual measured run separately.
- **Optional NVIDIA profile:** blocked until driver, CUDA compatibility, exact VRAM, host RAM, and an offline artifact manifest are recorded. GPU memory is never pooled with Mac or host RAM.

## Preflight gate for later phases

Before an import benchmark: capture free disk/RAM, CPU mode, thread count, package hashes, input hash/size, batch size, and worker count. Refuse a run when the configured workspace quota cannot hold raw data, temporary fragments, and final fragments. Linux correctness tests must pass in CPU mode; a GPU is optional acceleration only.
