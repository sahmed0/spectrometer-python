import machine
import rp2
import uctypes
import time
import sys
import select

# ==============================================================================
# Global Configuration & Hardware Pins
# ==============================================================================
LARMOR_FREQ = 2210.0  # Target Larmor frequency for Earth's Field NMR (Hz)

# GPIO Pin Assignments
LED_PIN = 25          # Onboard LED (Raspberry Pi Pico)
ADC_PIN = 28          # ADC Input 2 (GP28) - Connected to NMR coil amplifier output
PULSE_PIN = 16        # Output to H-Bridge/Transmitter for RF pulses
PP_COIL_PIN = 26      # Output to Pre-Polarization coil relay/MOSFET
DET_SWITCH_PIN = 22   # Output to Rx/Tx switching relay (Isolation switch)

# ==============================================================================
# PIO (Programmable I/O) Program: CPMG Sequence
# ==============================================================================
#
# WHAT IS PIO?
# The RP2040 has a dedicated hardware block called PIO (Programmable I/O).
# It runs small assembly programs independently of the main CPU.
#
# WHY USE PIO FOR NMR?
# 1. Microsecond Precision: Standard Python `sleep_us()` has jitter (random delays)
#    due to garbage collection and system interrupts. NMR requires pulse timing 
#    precision of < 1us to maintain phase coherence in the spin echoes.
#    PIO guarantees deterministic execution (+/- 1 clock cycle).
# 2. Speed: It can toggle pins much faster than CPU bit-banging.
#
# HOW IT WORKS:
# This program generates the CPMG (Carr-Purcell-Meiboom-Gill) pulse sequence:
# 90_pulse -> tau -> [180_pulse -> tau -> trigger_adc -> tau] * N_echoes
#
# The 'sideset' pin is the PULSE_PIN. .side(1) turns it ON, .side(0) turns it OFF.
#
@rp2.asm_pio(sideset_init=rp2.PIO.OUT_LOW)
def cpmg():
    # --- PHASE 1: Initial 90 degree Pulse ---
    pull()                  # Load 90_pulse duration (in cycles) from FIFO (CPU) to OSR
    mov(y, osr)             # Move value to Y scratch register
    
    nop().side(1)           # Turn PULSE ON
    label("loop_90")
    jmp(y_dec, "loop_90")   # Delay for duration Y
    nop().side(0)           # Turn PULSE OFF
    
    # --- PHASE 2: First Tau Delay ---
    # Wait time between 90 pulse and first 180 pulse
    pull()                  # Load tau duration from FIFO
    mov(y, osr)             # Move to Y
    label("loop_tau1")
    jmp(y_dec, "loop_tau1") # Delay
    
    # --- PHASE 3: Echo Loop Setup ---
    pull()                  # Load Loop Count (Number of Echoes)
    mov(x, osr)             # Move to X register (Loop Counter)
    
    pull()                  # Load 180_pulse duration
    mov(isr, osr)           # Save 180_pulse duration in ISR (Input Shift Register) for reuse
    
    pull()                  # Load Tau duration (for used inside the loop)
                            # OSR now holds 'Tau_len' and will stay there
                        
    # --- PHASE 4: The Echo Train (Repeated N times) ---
    label("echo_loop")
    
    # 1. Apply 180 Pulse
    mov(y, isr)             # Reload 180_len from ISR
    nop().side(1)           # Pulse ON
    label("loop_180")
    jmp(y_dec, "loop_180")  # Delay
    nop().side(0)           # Pulse OFF
    
    # 2. Wait Tau (180 -> Echo Center)
    mov(y, osr)             # Reload Tau from OSR
    label("loop_wait_1")
    jmp(y_dec, "loop_wait_1")
    
    # 3. TRIGGER ACQUISITION
    # This pulses the IRQ flag. The DMA controller is configured to wait 
    # for data from the ADC, which effectively syncs here.
    irq(0)                  
    
    # 4. Wait Tau (Echo Center -> Next 180)
    mov(y, osr)             # Reload Tau from OSR
    label("loop_wait_2")
    jmp(y_dec, "loop_wait_2")
    
    # 5. Repeat
    jmp(x_dec, "echo_loop") # Decrement X. If not 0, jump back to start of loop.


# ==============================================================================
# DMA (Direct Memory Access) Driver
# ==============================================================================
#
# WHAT IS DMA?
# DMA allows hardware peripherals (like the ADC) to write directly into memory
# (RAM) without involving the main CPU.
#
# WHY USE DMA FOR NMR?
# 1. Spectral Purity: The CPU reading ADC values in a loop (`val = adc.read_u16()`)
#    introduces "sampling jitter" because the time per loop iteration varies.
#    Variable sampling time distorts the frequency spectrum (FFT).
#    DMA transfers occur at perfect, hardware-clocked intervals.
# 2. High Throughput: DMA can capture samples at the maximum ADC speed (500ksps)
#    which is difficult to sustain with a Python loop.
#
class DMADriver:
    def __init__(self, channel=0):
        self.channel = channel
        self.DMA_BASE = 0x50000000
        self.ch_base = self.DMA_BASE + (0x40 * channel)
        
        # RP2040 DMA Register Offsets
        self.READ_ADDR = self.ch_base + 0x00    # Source address
        self.WRITE_ADDR = self.ch_base + 0x04   # Destination address
        self.TRANS_COUNT = self.ch_base + 0x08  # Number of transfers
        self.CTRL_TRIG = self.ch_base + 0x0C    # Control and Trigger
        self.AL1_CTRL = self.ch_base + 0x10     # Control (Write-only, no trigger)
        
        # ADC Base Address (for FIFO access)
        self.ADC_BASE = 0x4004c000
        self.ADC_FIFO = self.ADC_BASE + 0x08
        
    def config(self, buffer, count):
        """
        Configures the DMA channel to transfer 'count' samples from ADC FIFO to 'buffer'.
        """
        # Disable channel first to safely modify
        machine.mem32[self.CTRL_TRIG] = 0 
        
        # Configure Control Register (CTRL) capabilities:
        # EN(1): Enable DMA
        # DATA_SIZE(1): Transfer 16-bit values (matches ADC sample size)
        # INCR_READ(0): Do NOT increment source address (always read from fixed ADC_FIFO)
        # INCR_WRITE(1): DO increment destination address (fill the buffer)
        # TREQ_SEL(36): Transfer Request Signal = DREQ_ADC (Wait for ADC to have data)
        #
        # Bit mapping:
        # [0] EN = 1
        # [2:3] SIZE = 1 (0x1) -> 2 bytes
        # [4] INCR_READ = 0
        # [5] INCR_WRITE = 1
        # [15:20] TREQ_SEL = 36 (0x24)
        
        ctrl = 0
        ctrl |= (1 << 0)   # Enable
        ctrl |= (1 << 2)   # 16-bit
        ctrl |= (1 << 5)   # Incr Write
        ctrl |= (36 << 15) # DREQ_ADC (36 is the DREQ ID for ADC on RP2040)
        
        # Get physical address of the buffer
        driver_addr = uctypes.addressof(buffer)
        
        # Write configuration
        machine.mem32[self.READ_ADDR] = self.ADC_FIFO  # Read from ADC
        machine.mem32[self.WRITE_ADDR] = driver_addr   # Write to Buffer
        machine.mem32[self.TRANS_COUNT] = count        # How many samples
        
        # Write Control register (using Alias that doesn't trigger immediately, 
        # though DREQ logic means it will wait for ADC anyway)
        machine.mem32[self.AL1_CTRL] = ctrl

    def wait(self):
        """
        Blocks execution until the DMA transfer is complete.
        """
        # Check BUSY bit (Bit 24) in CTRL register
        while (machine.mem32[self.CTRL_TRIG] & (1<<24)):
            pass

    def disable(self):
        machine.mem32[self.CTRL_TRIG] = 0

# ==============================================================================
# Main Application Logic
# ==============================================================================
def main():
    # --- Hardware Setup ---
    led = machine.Pin(LED_PIN, machine.Pin.OUT)
    
    # Pulse pin is strictly controlled by PIO, but we define the object for setup
    pulse_pin = machine.Pin(PULSE_PIN) 
    
    pp_coil = machine.Pin(PP_COIL_PIN, machine.Pin.OUT) # Pre-polarization
    det_switch = machine.Pin(DET_SWITCH_PIN, machine.Pin.OUT) # Rx Isolation
    
    # ADC Setup
    adc = machine.ADC(ADC_PIN) 
    
    # --- ADC Hardware Register Setup ---
    # We need to access hardware registers to enable the FIFO and Request signals
    # that standard MicroPython `ADC` class doesn't expose deeply enough for DMA.
    ADC_BASE = 0x4004c000
    ADC_CS = ADC_BASE + 0x00   # Control and Status
    ADC_FCS = ADC_BASE + 0x0C  # FIFO Control and Status
    ADC_DIV = ADC_BASE + 0x10  # Clock Divider
    
    # Initialize PIO
    # standard frequency 125MHz ensures 1 cycle = 8ns.
    sm = rp2.StateMachine(0, cpmg, freq=125_000_000, sideset_base=pulse_pin)
    sm.active(1)

    # Initialize DMA
    dma = DMADriver(channel=0)
    
    print("EFNMR MicroPython Controller Ready")
    print("Waiting for commands (CPMG, FID)...")

    while True:
        # Non-blocking check for input commands
        if select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline().strip()
            if not line: continue
            
            parts = line.split(',')
            if len(parts) == 0: continue
            cmd = parts[0]
            
            if cmd == "CPMG" or cmd == "FID":
                try:
                    # Parse Parameters
                    # Command format: DATA_TYPE, SLEEP_TIME, DATA_SIZE, TAU_US, N_ECHOES
                    sleep_time = int(parts[1]) # Interval between samples (affects ADC rate)
                    req_datasize = int(parts[2]) # Requested number of samples
                    tau_us = int(parts[3])     # Tau delay (half echo spacing)
                    n_echoes = int(parts[4])   # Number of echoes
                except (IndexError, ValueError) as e:
                    print(f"Error: Invalid Arguments - {e}")
                    continue
                
                # --- Step 1: Pre-Polarization ---
                # Turn on the strong magnet coil to align spins
                led.value(1)
                pp_coil.value(1)
                time.sleep(3)       # Polarize for 3 seconds (adjust as needed for T1)
                pp_coil.value(0)    # Turn off quickly
                
                # --- Step 2: Calculate Timing & Cycles ---
                # Convert times to PIO clock cycles (125 cycles per microsecond at 125MHz)
                period = 1_000_000.0 / LARMOR_FREQ
                pulse_time = int(period / 4.0)          # 90 degree pulse approx 1/4 period
                
                pulse_cycles = int(pulse_time * 125)    # 90 deg pulse cycles
                pulse180_cycles = pulse_cycles * 2      # 180 deg pulse cycles
                tau_cycles = int(tau_us * 125)          # Tau delay cycles
                
                # --- Step 3: Configure ADC & DMA ---
                
                # Calculate ADC Clock Divider to match requested 'sleep_time'
                # ADC Base Clk = 48MHz. 
                # Formula: Sampling Rate = 48MHz / (DIV + 1)
                # We approximate: Div = (sleep_time_us * 48) - 1
                if sleep_time < 2: sleep_time = 2 # Clamp minimum speed
                div_val = (sleep_time * 48) - 1
                machine.mem32[ADC_DIV] = (div_val << 8) # Register takes 8.8 fixed point
                
                # Enable ADC FIFO and DREQ (Data Request)
                # FCS Register bits:
                # [0] EN = 1
                # [3] DREQ_EN = 1 (Request DMA when data available)
                # [24:27] THRESH = 1 (Trigger when at least 1 sample in FIFO)
                machine.mem32[ADC_FCS] = (1 << 0) | (1 << 3) | (1 << 24)
                
                # Determine memory buffer size
                # For CPMG, we ideally capture the entire echo train.
                # If 'FID', we might just capture `req_datasize`.
                # We trust the GUI/User to request a reasonable `datasize`.
                datasize = req_datasize
                if datasize > 20000: datasize = 20000 # Safety cap for RAM
                
                buf = bytearray(datasize * 2) # 16-bit samples = 2 bytes each
                
                # Configure DMA to fill this buffer
                dma.config(buf, datasize)
                
                # Configure ADC Input Mux
                # CS Register: Select Input 2 (GP28) -> Bits [12:14] = 2
                cs_val = machine.mem32[ADC_CS]
                cs_val &= ~(7 << 12) # Clear current mux
                cs_val |= (2 << 12)  # Set mux to 2
                cs_val |= (1 << 3)   # Set START_MANY (Continuous capture)
                machine.mem32[ADC_CS] = cs_val
                
                # --- Step 4: Sequence Execution ---
                det_switch.value(1) # Enable Rx Isolation (connect coil to amp)
                time.sleep_us(20)   # Allow relay/switch to settle
                
                # Reset PIO State Machine to ensure fresh start
                sm.active(0)
                sm.restart()
                sm.active(1)
                
                # Push parameters to PIO FIFO
                sm.put(pulse_cycles)    # 90 pulse length
                sm.put(tau_cycles)      # Tau length
                sm.put(n_echoes)        # Loop count
                sm.put(pulse180_cycles) # 180 pulse length
                
                # The PIO will now run.
                # It will trigger IRQ/Timings.
                # The DMA is waiting for ADC data.
                # The ADC is waiting for 'START_MANY' (which we set).
                # Note: In this architecture, ADC is free-running. Ideally, PIO 
                # implies strict sync. Synchronization here relies on the fact 
                # that we start them roughly together. For stricter sync, 
                # PIO can trigger the specific ADC conversion pin, but free-running
                # is often sufficient for basic CPMG envelopes.
                
                # Wait for DMA to complete (filling the buffer)
                dma.wait()
                
                # --- Step 5: Stop & Cleanup ---
                sm.active(0)                        # Stop PIO
                machine.mem32[ADC_CS] &= ~(1 << 3)  # Stop ADC (Clear START_MANY)
                dma.disable()                       # Stop DMA
                det_switch.value(0)                 # Disable Rx
                led.value(0)                        # LED Off
                
                # --- Step 6: Data Transmission ---
                # Send data back to PC.
                # Format: Time(us),Value
                # We reconstruct time based on the known interval.
                t_us = 0
                for i in range(datasize):
                    # Combine 2 bytes into 16-bit integer
                    val = buf[2*i] | (buf[2*i+1] << 8)
                    print(f"{t_us},{val}")
                    t_us += sleep_time

if __name__ == "__main__":
    main()
