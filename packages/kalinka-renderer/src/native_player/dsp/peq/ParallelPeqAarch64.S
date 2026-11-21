// Five parallel pole pairs. Each mode works one sample behind the previous
// mode, so the five independent output sums hide the FMA latency on Cortex-A72.
// Each input sample is loaded once and passed through all five modes.
//
// x0: five [b,a,c,d] records, x1: five histories at sizeof(State) stride,
// x2: input, x3: prior sum (null starts from d0 * input), x4: output,
// x5: positive sample count divisible by 10. The final sum can overwrite input.
// For each mode y += c*w1 + d*w2 and w = x + a*w1 + b*w2.
.text
.p2align 5
.global kalinka_peq_five_modes
.hidden kalinka_peq_five_modes
.type kalinka_peq_five_modes, %function
kalinka_peq_five_modes:
    stp d8, d9, [sp, #-64]!
    stp d10, d11, [sp, #16]
    stp d12, d13, [sp, #32]
    stp d14, d15, [sp, #48]
    fmov d30, d0
    ldp q0, q1, [x0]
    ldp q2, q3, [x0, #32]
    ldp q4, q5, [x0, #64]
    ldp q6, q7, [x0, #96]
    ldp q8, q9, [x0, #128]
    ldp d10, d11, [x1]
    ldp d12, d13, [x1, #32]
    ldp d14, d15, [x1, #64]
    ldp d16, d17, [x1, #96]
    ldp d18, d19, [x1, #128]

// v0--v9 pack two coefficients per register. d10--d19 rotate the two
// histories, d20--d24 hold the partial sums, d25--d29 hold the inputs.
// All register and offset calculations below are resolved by the assembler.
.altmacro
.macro load reg, ptr, offset
    ldur d\reg, [\ptr, #\offset]
.endm
.macro store reg, ptr, offset
    stur d\reg, [\ptr, #\offset]
.endm
.macro madd dst, state, coef, lane
    fmla d\dst, d\state, v\coef\().d[\lane]
.endm
.macro start_state w2, coef, input
    fmadd d\w2, d\w2, d\coef, d\input
.endm

.macro seed dst, input
    fmul d\dst, d\input, d30
.endm
.macro tick t, mask, initial
    .if (\mask % 2)
      .if \initial
        load %(25+(\t%5)), x2, %(8*(\t%10))
        seed %(20+(\t%5)), %(25+(\t%5))
      .else
        load %(20+(\t%5)), x3, %(8*(\t%10))
      .endif
    .endif
    .irp operation,0,1,2,3
      .irp stage,0,1,2,3,4
        .if ((\mask >> \stage) % 2)
          .set sample, \t-\stage
          .set w1, 10+2*\stage+(sample%2)
          .set w2, 10+2*\stage+(1-sample%2)
          .set sum, 20+sample%5
          .set input, 25+sample%5
          .if \operation == 0
            madd %sum, %w2, %(2*\stage+1), 1
          .elseif \operation == 1
            .if (\stage == 0) && (\initial == 0)
              load %input, x2, %(8*(\t%10))
            .endif
            start_state %w2, %(2*\stage), %input
          .elseif \operation == 2
            madd %sum, %w1, %(2*\stage+1), 0
          .else
            madd %w2, %w1, %(2*\stage), 1
          .endif
        .endif
      .endr
    .endr
    .if (\mask >> 4)
      store %(20+((\t-4)%5)), x4, %(8*((\t%10)-4))
    .endif
.endm

.macro bank initial
    tick 0, 1, \initial
    tick 1, 3, \initial
    tick 2, 7, \initial
    tick 3, 15, \initial
    .irp t,4,5,6,7,8,9
      tick \t, 31, \initial
    .endr
    add x2, x2, #80
    .if !\initial
      add x3, x3, #80
    .endif
    add x4, x4, #80
    subs x5, x5, #10
    b.eq .Ldrain\@
.p2align 5
.Lcycle\@:
    .irp t,10,11,12,13,14,15,16,17,18,19
      tick \t, 31, \initial
    .endr
    add x2, x2, #80
    .if !\initial
      add x3, x3, #80
    .endif
    add x4, x4, #80
    subs x5, x5, #10
    b.ne .Lcycle\@
.Ldrain\@:
    tick 10, 30, \initial
    tick 11, 28, \initial
    tick 12, 24, \initial
    tick 13, 16, \initial
.endm
    cbz x3, .Lfirst
    bank 0
    b .Lsave
.Lfirst:
    bank 1
.Lsave:
    stp d10, d11, [x1]
    stp d12, d13, [x1, #32]
    stp d14, d15, [x1, #64]
    stp d16, d17, [x1, #96]
    stp d18, d19, [x1, #128]
    ldp d14, d15, [sp, #48]
    ldp d12, d13, [sp, #32]
    ldp d10, d11, [sp, #16]
    ldp d8, d9, [sp], #64
    ret
.size kalinka_peq_five_modes, .-kalinka_peq_five_modes
.section .note.GNU-stack,"",%progbits
