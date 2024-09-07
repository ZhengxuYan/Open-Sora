import os

from flash_attn import flash_attn_qkvpacked_func
import torch
import torch.distributed as dist
from ring_flash_attn import ring_flash_attn_qkvpacked_func

import random
import time


def set_seed(rank, seed=42):
    seed = rank + seed
    random.seed(seed)             
    torch.manual_seed(seed)      
    torch.cuda.manual_seed(seed)  
    torch.cuda.manual_seed_all(seed) 


def log(msg, a, rank0_only=False):
    world_size = dist.get_world_size()
    rank = dist.get_rank()
    if rank0_only:
        if rank == 0:
            print(
                f"{msg}: "
                f"max {a.abs().max().item()}, "
                f"mean {a.abs().mean().item()}",
                flush=True,
            )
        return

    for i in range(world_size):
        if i == rank:
            if rank == 0:
                print(f"{msg}:")
            print(
                f"[{rank}] "
                f"max {a.abs().max().item()}, "
                f"mean {a.abs().mean().item()}",
                flush=True,
            )
        dist.barrier()


if __name__ == "__main__":
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    set_seed(rank)
    world_size = dist.get_world_size()
    dtype = torch.bfloat16
    device = torch.device(f"cuda:{rank}")

    batch_size = 30
    seqlen = 920
    nheads = 16
    d = 72
    dropout_p = 0
    causal = False
    deterministic = True

    assert seqlen % world_size == 0
    assert d % 8 == 0

    qkv = torch.randn(
        batch_size, seqlen, 3, nheads, d, device=device, dtype=dtype, requires_grad=True
    )
    dist.broadcast(qkv, src=0)

    dout = torch.randn(batch_size, seqlen, nheads, d, device=device, dtype=dtype)
    print("dout.shape", dout.shape)
    dist.broadcast(dout, src=0)

    local_qkv = qkv.chunk(world_size, dim=1)[rank].detach().clone()
    local_qkv.requires_grad = True
    local_dout = dout.chunk(world_size, dim=1)[rank].detach().clone()
    print("local_dout.shape", local_dout.shape)

    dist.barrier()
    if rank == 0:
        print("#" * 30)
        print("# forward:")
        print("#" * 30)

    # out, lse, _ = flash_attn_qkvpacked_func(
    #     qkv,
    #     dropout_p=dropout_p,
    #     causal=causal,
    #     window_size=(-1, -1),
    #     alibi_slopes=None,
    #     deterministic=deterministic,
    #     return_attn_probs=True,
    # )
    with torch.profiler.profile(
        activities=[
        torch.profiler.ProfilerActivity.CPU,
        torch.profiler.ProfilerActivity.CUDA,
        ],
        profile_memory=True
    ) as prof:
        start_time = time.time()
        out, lse, _ = flash_attn_qkvpacked_func(
            qkv,
            dropout_p=dropout_p,
            causal=causal,
            window_size=(-1, -1),
            alibi_slopes=None,
            deterministic=deterministic,
            return_attn_probs=True,
        )
        end_time = time.time()
        attn_time = end_time - start_time
    with open("flash_test.txt", "a") as text_file:
        text_file.write(prof.key_averages().table(sort_by="self_cuda_memory_usage"))
        text_file.write("\n")
        text_file.write(f"Attn Time: {attn_time}\n")

    local_out = out.chunk(world_size, dim=1)[rank]
    local_lse = lse.chunk(world_size, dim=-1)[rank]

    fn = ring_flash_attn_qkvpacked_func

    # ring_out, ring_lse, _ = fn(
    #     local_qkv,
    #     dropout_p=dropout_p,
    #     causal=causal,
    #     window_size=(-1, -1),
    #     alibi_slopes=None,
    #     deterministic=deterministic,
    #     return_attn_probs=True,
    # )
    with torch.profiler.profile(
        activities=[
        torch.profiler.ProfilerActivity.CPU,
        torch.profiler.ProfilerActivity.CUDA,
        ],
        profile_memory=True
    ) as prof:
        start_time = time.time()
        ring_out, ring_lse, _ = fn(
            local_qkv,
            dropout_p=dropout_p,
            causal=causal,
            window_size=(-1, -1),
            alibi_slopes=None,
            deterministic=deterministic,
            return_attn_probs=True,
        )
        end_time = time.time()
        attn_time = end_time - start_time
    with open("ring_test.txt", "a") as text_file:
        text_file.write(prof.key_averages().table(sort_by="self_cuda_memory_usage"))
        text_file.write("\n")
        text_file.write(f"Attn Time: {attn_time}\n")


    print("ring_out.shape", ring_out.shape)
    log("out", out, rank0_only=True)
    log("lse", lse, rank0_only=True)
    log("out diff", local_out - ring_out)
    log("lse diff", local_lse - ring_lse)

    dist.barrier()
    if rank == 0:
        print("#" * 30)
        print("# backward:")
        print("#" * 30)

    out.backward(dout)
    dqkv = qkv.grad
    local_dqkv = dqkv.chunk(world_size, dim=1)[rank]

    ring_out.backward(local_dout)
    ring_dqkv = local_qkv.grad

    log("local_dq", local_dqkv[:, :, 0, :])
    log("dq diff", local_dqkv[:, :, 0, :] - ring_dqkv[:, :, 0, :])

    log("local_dk", local_dqkv[:, :, 1, :])
    log("dk diff", local_dqkv[:, :, 1, :] - ring_dqkv[:, :, 1, :])

    log("local_dv", local_dqkv[:, :, 2, :])
    log("dv diff", local_dqkv[:, :, 2, :] - ring_dqkv[:, :, 2, :])
