from pyrtl import *

set_debug_mode()

DATWIDTH = 8
MATSIZE = 4
ACCSIZE = 512

def MAC(data_width, matrix_size, data_in, acc_in, switchw, weight_in, weight_we, weight_tag):
    '''Multiply-Accumulate unit with programmable weight.
    Inputs
    data_in: The 8-bit activation value to multiply by weight.
    acc_in: 32-bit value to accumulate with product.
    switchw: Control signal; when 1, switch to using the other weight buffer.
    weight_in: 8-bit value to write to the secondary weight buffer.
    weight_we: When high, weights are being written; if tag matches, store weights.
               Otherwise, pass them through with incremented tag.
    weight_tag: If equal to 255, weight is for this row; store it.

    Outputs
    out: Result of the multiply accumulate; moves one cell down to become acc_in.
    data_reg: data_in, stored in a pipeline register for cell to the right.
    switch_reg: switchw, stored in a pipeline register for cell to the right.
    weight_reg: weight_in, stored in a pipeline register for cell below.
    weight_we_reg: weight_we, stored in a pipeline register for cell below.
    weight_tag_reg: weight_tag, incremented and stored in a pipeline register for cell below
    '''
    
    # Check lengths of inupts
    if len(weight_in) != len(data_in) != data_width:
        raise Exception("Expected 8-bit value in MAC.")
    if len(switchw) != len(weight_we) != 1:
        raise Exception("Expected 1-bit control signal in MAC.")

    # Should never switch weight buffers while they're changing
    #rtl_assert(~(weight_we & switchw), Exception("Cannot switch weight values when they're being loaded!"))

    # Use two buffers to store weight and next weight to use.
    wbuf1, wbuf2 = Register(len(weight_in)), Register(len(weight_in))

    # Track which buffer is current and which is secondary.
    current_buffer_reg = Register(1)
    with conditional_assignment:
        with switchw:
            current_buffer_reg.next |= ~current_buffer_reg
    current_buffer = current_buffer_reg ^ switchw  # reflects change in same cycle switchw goes high

    # When told, store a new weight value in the secondary buffer
    with conditional_assignment:
        with weight_we & (weight_tag == Const(MATSIZE-1)):
            with current_buffer == 0:  # If 0, wbuf1 is current; if 1, wbuf2 is current
                wbuf2.next |= weight_in
            with otherwise:
                wbuf1.next |= weight_in

    # Do the actual MAC operation
    weight = select(current_buffer, wbuf2, wbuf1)
    probe(weight)
    product = weight * data_in
    out = product + acc_in
    if len(out) > 32:
        out = out[:32]
                
    # For values that need to be forward to the right/bottom, store in pipeline registers
    data_reg = Register(len(data_in))  # pipeline register, holds data value for cell to the right
    data_reg.next <<= data_in
    switch_reg = Register(1)  # pipeline register, holds switch control signal for cell to the right
    switch_reg.next <<= switchw
    acc_reg = Register(len(out))  # output value for MAC below
    acc_reg.next <<= out
    weight_reg = Register(len(weight_in))  # pipeline register, holds weight input for cell below
    weight_reg.next <<= weight_in
    weight_we_reg = Register(1)  # pipeline register, holds weight write enable signal for cell below
    weight_we_reg.next <<= weight_we
    weight_tag_reg = Register(len(weight_tag))  # pipeline register, holds weight tag for cell below
    weight_tag_reg.next <<= (weight_tag + 1)[:len(weight_tag)]  # increment tag as it passes down rows

    return acc_reg, data_reg, switch_reg, weight_reg, weight_we_reg, weight_tag_reg

    
def MMArray(data_width, matrix_size, data_in, new_weights, weights_in, weights_we):
    '''
    data_in: 256-array of 8-bit activation values from systolic_setup buffer
    new_weights: 256-array of 1-bit control values indicating that new weight should be used
    weights_in: output of weight FIFO (8 x matsize x matsize bit wire)
    weights_we: 1-bit signal to begin writing new weights into the matrix
    '''

    # For signals going to the right, store in a var; for signals going down, keep a list
    # For signals going down, keep a copy of inputs to top row to connect to later
    weights_in_top = [ WireVector(data_width) for i in range(matrix_size) ]  # input weights to top row
    weights_in_last = [x for x in weights_in_top]
    weights_enable_top = [ WireVector(1) for i in range(matrix_size) ]  # weight we to top row
    weights_enable = [x for x in weights_enable_top]
    weights_tag_top = [ WireVector(data_width) for i in range(matrix_size) ]  # weight row tag to top row
    weights_tag = [x for x in weights_tag_top]
    data_out = [Const(0) for i in range(matrix_size)]  # will hold output from final row
    # Build array of MACs
    for i in range(matrix_size):  # for each row
        din = probe(data_in[i])
        switchin = probe(new_weights[i])
        for j in range(matrix_size):  # for each column
            acc_out, din, switchin, newweight, newwe, newtag  = MAC(data_width, matrix_size, din, data_out[j], switchin, weights_in_last[j], weights_enable[j], weights_tag[j])
            probe(data_out[j], "MACacc{}_{}".format(i, j))
            probe(acc_out, "MACout{}_{}".format(i, j))
            probe(din, "MACdata{}_{}".format(i, j))
            weights_in_last[j] = newweight
            weights_enable[j] = newwe
            weights_tag[j] = newtag
            data_out[j] = acc_out
    
    # Handle weight reprogramming
    programming = probe(Register(1))  # when 1, we're in the process of loading new weights
    size = 1
    while pow(2, size) < MATSIZE:
        size = size + 1
    progstep = probe(Register(size))  # 256 steps to program new weights (also serves as tag input)
    with conditional_assignment:
        with weights_we & (~programming):
            programming.next |= 1
        with programming & (progstep == MATSIZE-1):
            programming.next |= 0
        with otherwise:
            pass
        with programming:  # while programming, increment state each cycle
            progstep.next |= progstep + 1
        with otherwise:
            progstep.next |= Const(0)

    # Divide FIFO output into rows (each row datawidth x matrixsize bits)
    rowsize = data_width * matrix_size
    weight_arr = [ weights_in[i*rowsize : i*rowsize + rowsize] for i in range(matrix_size) ]
    # Mux the wire for this row
    current_weights_wire = mux(progstep, *weight_arr)
    # Split the wire into an array of 8-bit values
    current_weights = [ current_weights_wire[i*data_width:i*data_width+data_width] for i in range(matrix_size) ]

    # Connect top row to input and control signals
    for i, win in enumerate(weights_in_top):
        # From the current 256-array, select the byte for this column
        win <<= current_weights[i]
    for we in weights_enable_top:
        # Whole row gets same signal: high when programming new weights
        we <<= programming
    for wt in weights_tag_top:
        # Tag is same for whole row; use state index (runs from 0 to 255)
        wt <<= progstep

    return data_out


def accum(size, data_in, waddr, wen, wclear, raddr, lastvec):
    '''A single 32-bit accumulator with 2^size 32-bit buffers.
    On wen, writes data_in to the specified address (waddr) if wclear is high;
    otherwise, it performs an accumulate at the specified address (buffer[waddr] += data_in).
    lastvec is a control signal indicating that the operation being stored now is the
    last vector of a matrix multiply instruction (at the final accumulator, this becomes
    a "done" signal).
    '''

    mem = MemBlock(bitwidth=32, addrwidth=size)

    # Writes
    with conditional_assignment:
        with wen:
            with wclear:
                mem[waddr] |= data_in
            with otherwise:
                mem[waddr] |= (data_in + mem[waddr])[:mem.bitwidth]

    # Read
    data_out = mem[raddr]

    # Pipeline registers
    waddrsave = Register(len(waddr))
    waddrsave.next <<= waddr
    wensave = Register(1)
    wensave.next <<= wen
    wclearsave = Register(1)
    wclearsave.next <<= wclear
    lastsave = Register(1)
    lastsave.next <<= lastvec

    return data_out, waddrsave, wensave, wclearsave, lastsave

def accumulators(accsize, datas_in, waddr, we, wclear, raddr, lastvec):
    '''
    Produces array of accumulators of same dimension as datas_in.
    '''

    accout = [ None for i in range(len(datas_in)) ]
    waddrin = waddr
    wein = we
    wclearin = wclear
    lastvecin = lastvec
    for i,x in enumerate(datas_in):
        dout, waddrin, wein, wclearin, lastvecin = accum(accsize, x, waddrin, wein, wclearin, raddr, lastvecin)
        accout[i] = dout
        done = lastvecin

    return accout, done


def FIFO(matsize, mem_data, mem_valid, next_tile):
    '''
    matsize is the length of one row of the Matrix.
    mem_data is the connection from the DRAM controller, which is assumed to be 64 bytes wide.
    mem_valid is a one bit control signal from the controller indicating that the read completed and the current value is valid.
    next_tile signals to drop the tile at the end of the FIFO and advance everything forward.

    Output
    tile, ready, full
    tile: entire tile at the front of the queue (8 x matsize x matsize bits)
    ready: the tile output is valid
    full: there is no room in the FIFO
    '''
    
    # Make some size parameters, declare state register
    totalsize = matsize * matsize  # total size of a tile in bytes
    tilesize = totalsize * 8
    ddrwidth = len(mem_data)/8  # width from DDR in bytes (typically 64)
    size = 1
    while pow(2, size) < (totalsize/ddrwidth):
        size = size << 1
    state = Register(size)  # Number of reads to receive (each read is ddrwidth bytes)

    # Declare top row of buffer: need to write to it in ddrwidth-byte chunks
    topbuf = [ Register(ddrwidth*8) for i in range(max(1, totalsize/ddrwidth)) ]

    # When we get data from DRAM controller, write to correct buffer space
    with conditional_assignment:
        with mem_valid:
            state.next |= state + 1  # state tracks which ddrwidth-byte chunk we're writing to
            for i, reg in enumerate(topbuf):  # enumerate a decoder for write-enable signals
                with state == Const(i):
                    reg.next <<= mem_data

    # Track when first buffer is filled and when data moves out of it
    full = Register(1)  # goes high when last chunk of top buffer is filled
    cleartop = WireVector(1)
    with conditional_assignment:
        with mem_valid & (state == Const(len(topbuf))):
            full.next |= 1
        with cleartop:
            full.next |= 0

    # Build buffers for remainder of FIFO
    buf2, buf3, buf4 = Register(tilesize), Register(tilesize), Register(tilesize)
    # If a given row is empty, track that so we can fill immediately
    empty2, empty3, empty4 = Register(1), Register(1), Register(1)

    # Handle moving data between the buffers
    with conditional_assignment:
        with full & empty2:
            buf2.next |= concat_list(topbuf)
            cleartop |= 1
            empty2.next |= 0
        with empty3 & ~empty2:
            buf3.next |= buf2
            empty3.next |= 0
            empty2.next |= 1
        with empty4 & ~empty3:
            buf4.next |= buf3
            empty4.next |= 0
            empty3.next |= 1
    
    ready = (~empty4) & (~next_tile)  # there is data in final buffer and we're not about to change it

    return buf4, ready, full

def systolic_setup(matsize, vec_in, waddr, valid, clearbit, lastvec, switch):
    '''Buffers vectors from the unified SRAM buffer so that they can be fed along diagonals to the
    Matrix Multiply array.

    matsize: row size of Matrix
    vec_in: row read from unified buffer
    waddr: the accumulator address this vector is bound for
    valid: this is a valid vector; write it when done
    clearbit: if 1, store result (default accumulate)
    lastvec: this is the last vector of a matrix
    switch: use the next weights tile beginning with this vector

    Output
    next_row: diagonal cross-cut of vectors to feed to MM array
    switchout: switch signals for MM array
    addrout: write address for first accumulator
    weout: write enable for first accumulator
    clearout: clear signal for first accumulator
    doneout: done signal for first accumulator
    '''

    # Use a diagonal set of buffer so that when a vector is read from SRAM, it "falls" into
    # the correct diagonal pattern.
    # The last column of buffers need extra bits for control signals, which propagate down
    # and into the accumulators.

    addrreg = Register(len(waddr))
    addrreg.next <<= waddr
    wereg = Register(1)
    wereg.next <<= valid
    clearreg = Register(1)
    clearreg.next <<= clearbit
    donereg = Register(1)
    donereg.next <<= lastvec
    topreg = Register(DATWIDTH)

    firstcolumn = [topreg,] + [ Register(DATWIDTH) for i in range(matsize-1) ]
    lastcolumn = [ None for i in range(matsize) ]
    lastcolumn[0] = topreg

    # Generate switch signals to matrix; propagate down
    switchout = [ None for i in range(matsize) ]
    switchout[0] = Register(1)
    switchout[0].next <<= switch
    for i in range(1, len(switchout)):
        switchout[i] = Register(1)
        switchout[i].next <<= switchout[i-1]

    # Generate control pipeline for address, clear, and done signals
    addrout = addrreg
    weout = wereg
    clearout = clearreg
    doneout = lastvec
    # Need one extra cycle of delay for control signals before giving them to first accumulator
    # But we already did registers for first row, so cancels out
    for i in range(0, matsize):
        a = Register(len(addrout))
        a.next <<= addrout
        addrout = a
        w = Register(1)
        w.next <<= weout
        weout = w
        c = Register(1)
        c.next <<= clearout
        clearout = c
        d = Register(1)
        d.next <<= doneout
        doneout = d

    # Generate buffers in a diagonal pattern
    for row in range(1, matsize):  # first row is done
        left = firstcolumn[row]
        lastcolumn[row] = left
        for column in range(0, row):  # first column is done
            buf = Register(DATWIDTH)
            buf.next <<= left
            left = buf
            lastcolumn[row] = left  # holds final column for output

    # Connect first column to input data
    datain = [ vec_in[i*DATWIDTH : i*DATWIDTH+DATWIDTH] for i in range(matsize) ]
    for din, reg in zip(datain, firstcolumn):
        reg.next <<= din
    
        
    return lastcolumn, switchout, addrout, weout, clearout, doneout


def MMU(data_width, matrix_size, accum_size, vector_in, accum_raddr, accum_waddr, vec_valid, accum_overwrite, lastvec, switch_weights, ddr_data, ddr_valid):

    logn1 = 1
    while pow(2, logn1) < (matrix_size + 1):
        logn1 = logn1 << 1
    logn = 1
    while pow(2, logn) < (matrix_size):
        logn = logn << 1
    weights_wait = Register(logn1)
    weights_count = Register(logn)
    startup = Register(1)
    startup.next <<= 1  # 0 only in first cycle
    weights_we = WireVector(1)
    done_programming = WireVector(1)

    rtl_assert(~(switch_weights & (weights_wait != 0)), Exception("Weights are not ready to switch. Need a minimum of {} + 1 cycles since last switch.".format(matrix_size)))
    
    weights_tile, tile_ready, full = FIFO(matsize=matrix_size, mem_data=ddr_data, mem_valid=ddr_valid, next_tile=done_programming)
    
    matin, switchout, addrout, weout, clearout, doneout = systolic_setup(matsize=matrix_size, vec_in=vector_in, waddr=accum_waddr, valid=vec_valid, clearbit=accum_overwrite, lastvec=lastvec, switch=switch_weights)

    mouts = MMArray(data_width=data_width, matrix_size=matrix_size, data_in=matin, new_weights=switchout, weights_in=weights_tile, weights_we=weights_we)

    accout, done = accumulators(accsize=accum_size, datas_in=mouts, waddr=addrout, we=weout, wclear=clearout, raddr=accum_raddr, lastvec=doneout)

    totalwait = Const(matrix_size + 1)
    
    with conditional_assignment:
        with startup == 0:  # When we start, we're ready to push weights as soon as FIFO is ready
            weights_wait.next |= totalwait
        with switch_weights:  # Got a switch signal; start wait count
            weights_wait.next |= 1  
        with weights_wait != totalwait:  # Stall on the final number
            weights_wait.next |= weights_wait + 1
        with weights_count != 0:  # If we've started programming new weights, reset
            weights_wait.next |= 0
        with otherwise:
            pass

        with (weights_wait == totalwait) & tile_ready:  # Ready to push new weights in
            weights_count.next |= 1
        with weights_count == Const(matrix_size):  # Finished pushing new weights
            done_programming |= 1
            weights_count.next |= 0
        with otherwise:  # We're pushing weights now; increment count
            weights_count.next |= weights_count + 1
            weights_we |= 1

    return accout, done

'''
Do we need full/stall signal from Matrix? Would need to stop SRAM out from writing to systolic setup
Yes: MMU needs to track when both buffers used and emit such a signal

Control signals propagating down systolic_setup to accumulators:
-Overwrite signal (default: accumulate)
-New accumulator address value (default: add 1 to previous address)
-Done signal?
'''

def testall(input_vectors, weights):
    L = len(input_vectors)
    
    ins = [probe(Input(DATWIDTH)) for i in range(MATSIZE)]
    invec = concat_list(ins)
    swap = Input(1, 'swap')
    ws = [ Const(item, bitwidth=DATWIDTH) for sublist in weights for item in sublist ]  # flatten weight matrix
    waddr = Input(8)
    lastvec = Input(1)
    valid = Input(1)
    raddr = Input(12, "raddr")
    donesig = Output(1, "done")

    outs = [Output(32, name="out{}".format(str(i))) for i in range(MATSIZE)]

    memdata = Input(64*8)
    memvalid = Input(1)
    
    accout, done = MMU(data_width=DATWIDTH, matrix_size=MATSIZE, accum_size=ACCSIZE, vector_in=invec, accum_raddr=raddr, accum_waddr=waddr, vec_valid=valid, accum_overwrite=Const(0), lastvec=lastvec, switch_weights=swap, ddr_data=memdata, ddr_valid=memvalid)

    donesig <<= done
    for out, accout in zip(outs, accout):
        out <<= accout

    sim_trace = SimulationTrace()
    sim = FastSimulation(tracer=sim_trace)

    # First, simulate memory read to feed weights to FIFO
    ws = concat_list(ws)  # combine weights into single wire
    chunk = 64*8  # size of one dram read
    ws = [ ws[i*chunk:i*chunk+chunk] for i in range(len(ws)/chunk) ]  # divide weights into dram chunks
    for block in ws:
        d = {ins[j] : 0 for j in range(MATSIZE) }
        d.update({ we : 0, swap : 0, waddr : 0, lastvec : 0, valid : 0, raddr : 0, memdata : block, memvalid : 1 })
        sim.step(d)

    # Wait until the FIFO is ready
    for i in range(10):
        sim.step()
    
    # Send signal to write weights
    d = {ins[j] : 0 for j in range(MATSIZE) }
    d.update({ we : 1, swap : 0, waddr : 0, lastvec : 0, valid : 0, raddr : 0 })
    sim.step(d)

    # Wait MATSIZE cycles for weights to propagate
    for i in range(MATSIZE):
        d = {ins[j] : 0 for j in range(MATSIZE) }
        d.update({ we : 0, swap : 0, waddr : 0, lastvec : 0, valid : 0, raddr : 0 })
        sim.step(d)

    # Send the swap signal with first row of input
    d = {ins[j] : input_vectors[0][j] for j in range(MATSIZE) }
    d.update({ we : 0, swap : 1, waddr : 0, lastvec : 0, valid : 1, raddr : 0 })
    sim.step(d)

    # Send rest of input
    for i in range(L-1):
        d = {ins[j] : input_vectors[i+1][j] for j in range(MATSIZE) }
        d.update({ we : 0, swap : 0, waddr : i+1, lastvec : 1 if i == L-2 else 0, valid : 1, raddr : 0 })
        sim.step(d)

    # Wait some cycles while it propagates
    for i in range(L*2):
        d = {ins[j] : 0 for j in range(MATSIZE) }
        d.update({ we : 0, swap : 0, waddr : 0, lastvec : 0, valid : 0, raddr : 0 })
        sim.step(d)

    # Read out values
    for i in range(L):
        d = {ins[j] : 0 for j in range(MATSIZE) }
        d.update({ we : 0, swap : 0, waddr : 0, lastvec : 0, valid : 0, raddr : i })
        sim.step(d)

    with open('trace.vcd', 'w') as f:
        sim_trace.print_vcd(f)


#weights = [[1, 10, 10, 2], [3, 9, 6, 2], [6, 8, 2, 8], [4, 1, 8, 6]]  # transposed
#weights = [[4, 1, 8, 6], [6, 8, 2, 8], [3, 9, 6, 2], [1, 10, 10, 2]]  # tranposed, reversed
#weights = [[1, 3, 6, 4], [10, 9, 8, 1], [10, 6, 2, 8], [2, 2, 8, 6]]
weights = [[2, 2, 8, 6], [10, 6, 2, 8], [10, 9, 8, 1], [1, 3, 6, 4]]  # reversed

vectors = [[12, 7, 2, 6], [21, 21, 18, 8], [1, 4, 18, 11], [6, 3, 25, 15], [21, 12, 1, 15], [1, 6, 13, 8], [24, 25, 18, 1], [2, 5, 13, 6], [19, 3, 1, 17], [25, 10, 20, 10]]

testall(vectors, weights)