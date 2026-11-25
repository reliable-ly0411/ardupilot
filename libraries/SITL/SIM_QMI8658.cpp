#include "SIM_QMI8658.h"

#if AP_SIM_QMI8658_ENABLED
#include "SITL.h"

int SITL::QMI8658::rdwr(I2C::i2c_rdwr_ioctl_data *&data)
{
    const auto &request = data->msgs[0];
    if (!request.len || request.flags) {
        return -1;
    }
    const uint8_t reg = request.buf[0];
    if (data->nmsgs == 1 && request.len == 2) {
        const uint8_t value = request.buf[1];
        if (reg == 0x60 && value == 0xb0) {
            memset(registers, 0, sizeof(registers));
            locked = gating_disabled = false;
            sample_time = 0;
            return 0;
        }
        if (reg >= sizeof(registers)) {
            return -1;
        }
        registers[reg] = value;
        if (reg == 0x0a && value == 0x12) {
            gating_disabled = (registers[0x0b] == 1);
        }
        return 0;
    }
    if (data->nmsgs != 2 || request.len != 1 || data->msgs[1].flags != I2C_M_RD) {
        return -1;
    }
    const auto &reply = data->msgs[1];
    if (reg == 0 && reply.len == 1) {
        reply.buf[0] = 5;
        return 0;
    }
    if (reg == 0x2d && reply.len == 1) {
        const uint64_t now = AP_HAL::micros64();
        if (!locked && gating_disabled && registers[8] == 0x83 &&
            registers[3] == 0x33 && registers[4] == 0x73 && now - sample_time >= 1115) {
            const auto &state = AP::sitl()->state;
            // Model chip X forward, Y left, Z up (ROTATION_ROLL_180).
            const float a = 2048.0f / GRAVITY_MSS;
            const double counts[] = {25.0f * 256, state.xAccel * a, -state.yAccel * a,
                                    -state.zAccel * a, state.rollRate * 16,
                                    -state.pitchRate * 16, -state.yawRate * 16};
            for (uint8_t i = 0; i < ARRAY_SIZE(sample); i++) {
                sample[i] = int16_t(constrain_float(counts[i], -32768, 32767));
            }
            sample_time = now - (now - sample_time) % 1115;
            locked = true;
        }
        reply.buf[0] = (registers[0x0a] == 0x12 ? 0x80 : 0) | (locked ? 3 : 0);
        return 0;
    }
    if (reg == 0x33 && reply.len == sizeof(sample) && locked && (registers[2] & 0x40)) {
        for (uint8_t i = 0; i < ARRAY_SIZE(sample); i++) {
            const uint16_t word = uint16_t(sample[i]);
            reply.buf[2*i] = uint8_t(word);
            reply.buf[2*i+1] = uint8_t(word >> 8);
        }
        locked = false;
        return 0;
    }
    return -1;
}
#endif
