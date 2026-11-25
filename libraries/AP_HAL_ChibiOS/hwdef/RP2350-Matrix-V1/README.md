# Waveshare RP2350-Matrix V1 · ArduCopter Quad X

本目标基于 [ArduPilot RP2350 移植 PR #32995](https://github.com/ArduPilot/ardupilot/pull/32995) 的 `5e97be2505`。开发板使用 RP2350A、12 MHz 晶振、16 MiB W25Q128JV 和六轴 QMI8658。板号 `54071` 是此 fork 的开发用 APJ ID。板上的 WS2812 矩阵数据线 GP25 留作板载用途。板卡资料见[原厂原理图](https://files.waveshare.com/wiki/RP2350-Matrix/RP2350-Matrix.pdf)和[产品 Wiki](https://www.waveshare.com/wiki/RP2350-Matrix)。

## 接线

表中数字均为板上 **GP 编号**。串口顺序均为飞控 TX、飞控 RX，与对端交叉连接。各信号使用 3.3 V 逻辑并共地。舵机和电调的动力电源单独供电。

| AP 接口 | 引脚 | 使用方式 |
| --- | --- | --- |
| SERVO1–8 | GP10、11、12、13、14、15、16、17 | 板边连续八点，依次对应通道 1–8。四旋翼用前四路，辅助输出通过 SERVO5_FUNCTION 至 SERVO8_FUNCTION 分配 |
| SERIAL0 | USB-C | USB CDC 地面站 |
| SERIAL1，UART0 | GP18、GP19 | MAVLink 2 |
| SERIAL2，UART1 | GP26、GP27 | GPS |
| SERIAL3，PIOUART0 | GP22、GP23 | PIO0 的 SM0/1，按用途设置 SERIAL3_PROTOCOL、SERIAL3_BAUD |
| SERIAL4，PIOUART1 | GP20、GP21 | 底面 TP10、TP11，PIO0 的 SM2/3，按用途设置 SERIAL4_PROTOCOL、SERIAL4_BAUD |
| 外接 I²C，AP 总线 1 | GP28 SDA、GP29 SCL | I2C0。模块需要 3.3 V 上拉，可接 BMP280（0x76 或 0x77） |
| 板载 IMU，AP 总线 0 | GP6 SDA、GP7 SCL | I2C1 私有总线，QMI8658 地址 0x6B。GP8/9 是 IMU 中断线，驱动使用同步锁存轮询 |
| 通用 GPIO | GP24、GP0、GP1 | AP GPIO 58、59、60，上电下拉输入。GP0/1 在底面 TP4/5 |

每两路相邻 PWM 共用一个周期，1/2、3/4、5/6、7/8 分别使用 slice 5、6、7、0。GPIO 模式的 AP 编号为 50–57。两组 PIO 串口默认协议为 None，可分别启用。改好串口参数后重启。

## 构建和烧录

在仓库根目录使用本分支锁定的子模块和 ArduPilot 构建环境。

```bash
git submodule update --init --recursive
python3 Tools/scripts/build_bootloaders.py RP2350-Matrix-V1
./waf configure --board RP2350-Matrix-V1
./waf copter
```

首次烧录先进入 BOOTSEL，把 `Tools/bootloaders/RP2350-Matrix-V1_bl.uf2` 拷到板卡的 USB 盘。再次进入 BOOTSEL，拷贝 `build/RP2350-Matrix-V1/bin/arducopter.uf2`。后续也可由地面站通过 USB 上传同目录的 `arducopter.apj`。

| XIP 地址 | 用途 |
| --- | --- |
| 0x10000000–0x10007FFF | AP bootloader |
| 0x10008000–0x1000FFFF | 参数存储保留区 |
| 0x10010000–0x10FFFFFF | Copter 应用区域 |

## 软件仿真

以下配置在主机上运行 Quad X 物理模型。SITL 通过 I²C 读写 QMI8658 模型并运行与板端同一份驱动。测试从 MAVLink 核对加速度计和陀螺仪的设备 ID、静止重力、角速度和姿态。

```bash
board=libraries/AP_HAL_ChibiOS/hwdef/RP2350-Matrix-V1
./waf configure --board sitl --extra-hwdef="$board/sitl-extra.dat"
./waf copter
python3 "$board/test_sitl.py" build/sitl/bin/arducopter --flight
```

## 实板台架验收

1. USB 上电，确认地面站显示 Quad X。记录 INS_ACC_ID 和 INS_GYR_ID，两者预期均为 0x456B01。当前采用传感器 X 向前、Y 向左、Z 向上的安装假设。用六面校准及逐轴手动滚转、俯仰和偏航确定实际板头，检查地面站姿态与转动同向。
2. 保存一个可恢复的参数，断电重启复读。检查 IMU 实际更新率、轴向、I²C 错误和总线上升时间。模型按 896.8 Hz 标称 ODR 工作，不能替代板上时序测量。
3. 断开动力，用逻辑分析仪依次检查八个 PWM 焊盘的通道次序、脉宽及同组频率。依次在两组硬件 UART 和两组 PIO UART 做收发回环，再同时检查两个 PIO 端口。用已知 I²C 模块验证 GP28/29。
4. 开发板仅有六轴 IMU。按测试场景连接气压计、定位及航向传感器，保留正常的 ArduCopter 解锁检查。物理机上的 Flash 擦写、外设电平、IMU 安装方向以及与外部仿真器的闭环均由实板验收确认。
