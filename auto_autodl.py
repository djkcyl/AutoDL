import time
import click
import httpx
import hashlib

from loguru import logger
from noneprompt import InputPrompt, ListPrompt, Choice, CancelledError


uss_list = ["北京A区", "芜湖区", "内蒙A区", "A100专区", "佛山区", "毕业季A区", "南京新手区", "特惠/泉州A区"]
AUTH = ""


@click.command()
@click.argument("username", type=int, default=0)
@click.argument("password", type=str, default="")
@click.argument("gpu", type=str, default="")
def main(username, password, gpu):
    global AUTH
    try:
        if not username:
            username = InputPrompt("请输入账号").prompt()
        if not password:
            password = InputPrompt("请输入密码", is_password=True).prompt()
    except CancelledError:
        click.secho("用户取消输入", fg="red", bold=True)
        return
    AUTH = login(username, password)

    if not AUTH:
        return

    wallet = httpx.get(
        "https://www.autodl.com/api/v1/wallet", headers={"authorization": AUTH}
    ).json()
    balance = wallet["data"]["assets"] / 1000
    if balance < 1:
        logger.error(f"账号余额不足：{balance} 元，请充值后再试")
        return

    instance_count = httpx.get(
        "https://www.autodl.com/api/v1/instance/count/v1",
        headers={"authorization": AUTH},
    ).json()["data"]

    regions = [
        r
        for r in httpx.get("https://www.autodl.com/api/v1/region/list").json()["data"]
        if r["region_name"] in uss_list
    ]

    gpu_types = httpx.post(
        "https://www.autodl.com/api/v1/machine/region/gpu_type",
        json={"region_sign_list": [x["region_sign"] for x in regions]},
        headers={"authorization": AUTH},
    ).json()["data"]
    gpu_types: list[dict] = sorted(gpu_types, key=lambda x: list(x.keys())[0])
    gpu_types = [list(x.keys())[0] for x in gpu_types]

    if not instance_count:
        logger.info("你的账号当前没有实例，正在为你抢购......")
        if not gpu:
            try:
                gpu = (
                    ListPrompt(
                        f"请选择要抢购的 GPU 型号，你当前余额为：{balance} 元",
                        choices=[Choice(x) for x in list(gpu_types)],
                        default_select=9,
                    )
                    .prompt()
                    .name
                )
            except CancelledError:
                click.secho("用户取消输入", fg="red", bold=True)
                return
        if gpu not in gpu_types:
            logger.error(f"没有该GPU型号：{gpu}")
            return
        while True:
            if can_use_machine := get_can_use_machine(gpu, regions):
                logger.info("正在创建实例......")
                buy_machine(can_use_machine, balance)
            else:
                logger.error("没有可用机器，2 秒后重试")
                time.sleep(2)
    else:
        logger.info("你的账号当前有已存在的实例，跳过抢购")
        return


def buy_machine(can_use_machine, balance):
    for machine in can_use_machine:
        price = machine["machine_sku_info"][0]["level_config"][-1]["discounted_price"] / 1000
        logger.info(
            f"机器名: {machine['machine_alias']}，区域: {machine['region_name']}，型号: {machine['gpu_name']}，剩余: {machine['gpu_idle_num']}，价格: {price}/时"
        )
        can_use_time = balance / price
        logger.warning(f"你当前的余额可购买 {can_use_time:.2f} 小时")
        logger.info(f"实例数量: {machine['max_instance_num']} / {machine['binding_instance_num']}")
        if float(machine["highest_cuda_version"]) >= 11.8:
            cuda = "cuda11.8"
        elif float(machine["highest_cuda_version"]) >= 11.6:
            cuda = "cuda11.6"
        else:
            cuda = "cudagl11.3"
        logger.info(f"机器最高支持cuda版本: {machine['highest_cuda_version']}，使用cuda版本: {cuda}")
        create = httpx.post(
            "https://www.autodl.com/api/v1/order/instance/create/payg",
            headers={"authorization": AUTH},
            json={
                "instance_info": {
                    "machine_id": machine["machine_id"],
                    "charge_type": "payg",
                    "req_gpu_amount": 1,
                    "image": f"hub.kce.ksyun.com/autodl-image/miniconda:{cuda}-cudnn8-devel-ubuntu20.04-py38",
                    "private_image_uuid": "",
                    "reproduction_uuid": "",
                    "instance_name": "",
                    "expand_data_disk": 0,
                },
                "price_info": {
                    "coupon_id_list": [],
                    "machine_id": machine["machine_id"],
                    "charge_type": "payg",
                    "duration": 1,
                    "num": 1,
                    "expand_data_disk": 0,
                },
            },
        ).json()
        if create["code"] == "Success":
            logger.success(f"购买成功：{create['data']}")
            break
        else:
            logger.error(f"购买失败：{create['msg']}")
            return

    while True:
        instance_list = httpx.post(
            "https://www.autodl.com/api/v1/instance",
            headers={"authorization": AUTH},
            json={"page_index": 1, "page_size": 500, "default_order": True},
        ).json()["data"]

        for instance in instance_list["list"]:
            logger.info(
                f"实例名: {instance['machine_alias']}，区域: {instance['region_name']}，GPU: {instance['snapshot_gpu_alias_name']} x {instance['req_gpu_amount']}，状态: {instance['status']}"
            )
            if instance["status"] == "running":
                logger.success("实例已启动，退出程序")
                exit()

        time.sleep(2)


def get_can_use_machine(gpu, regions):
    machine_list = httpx.post(
        "https://www.autodl.com/api/v1/user/machine/list",
        headers={"authorization": AUTH},
        json={
            "region_sign_list": [x["region_sign"] for x in regions],
            "charge_type": "payg",
            "page_index": 1,
            "page_size": 500,
            "default_order": True,
            # "gpu_idle_type": "desc",
            "gpu_type_name": [gpu],
        },
    ).json()["data"]

    machine_list["list"] = sorted(
        machine_list["list"],
        key=lambda x: (
            x["max_instance_num"] - x["binding_instance_num"],
            x["machine_sku_info"][0]["level_config"][-1]["discounted_price"],
        ),
    )

    can_use_machine = []
    logger.info(f"查询到的机器数量: {len(machine_list['list'])}")
    for machine in machine_list["list"]:
        if machine["gpu_idle_num"] > 0 and machine["gpu_order_num"] > 0:
            machine_info = f"机器名: {machine['machine_alias']}，区域: {machine['region_name']}，型号: {machine['gpu_name']}，剩余: {machine['gpu_idle_num']}，价格: {machine['machine_sku_info'][0]['level_config'][-1]['discounted_price']/1000}/时"
            if machine["max_instance_num"] > machine["binding_instance_num"]:
                logger.info(machine_info)
                can_use_machine.append(machine)
            else:
                logger.warning(machine_info)
    return can_use_machine


def login(username, password):
    count = httpx.post(
        "https://www.autodl.com/api/v1/login_failed/count", json={"username": username}
    ).json()
    if count["data"] > 1:
        logger.critical("密码忘了就先去浏览器试下吧，再试小心给你号封了，记得 5 分钟之后再用脚本登录！")
        return
    new_login = httpx.post(
        "https://www.autodl.com/api/v1/new_login",
        json={
            "phone": str(username),
            "password": hashlib.sha1(password.encode("utf-8")).hexdigest(),
            "v_code": "",
            "phone_area": "+86",
            "picture_id": None,
        },
    ).json()
    if new_login["code"] == "Success":
        logger.success(
            f"登录成功: {new_login['data']['user']['username']}（{new_login['data']['user']['id']}）"
        )
        ticket = new_login["data"]["ticket"]
    else:
        logger.error(f"登录失败: {new_login['msg']}")
        return
    passport = httpx.post(
        "https://www.autodl.com/api/v1/passport", json={"ticket": ticket}
    ).json()
    # if datetime.datetime.now() > datetime.datetime(2023, 3, 9, 19, 14, 45):
    #     logger.error("获取凭证失败: 未知错误")
    #     return
    if passport["code"] == "Success":
        token = passport["data"]["token"]
        logger.success(f"获取凭证成功: {token[:6]}******{token[-6:]}")
        return token
    else:
        logger.error(f"获取凭证失败: {passport['msg']}")
        return


if __name__ == "__main__":
    click.secho(
        """
    ___         __           ___         __        ____  __
   /   | __  __/ /_____     /   | __  __/ /_____  / __ \/ /
  / /| |/ / / / __/ __ \   / /| |/ / / / __/ __ \/ / / / /
 / ___ / /_/ / /_/ /_/ /  / ___ / /_/ / /_/ /_/ / /_/ / /___
/_/  |_\__,_/\__/\____/  /_/  |_\__,_/\__/\____/_____/_____/
""",
        fg="bright_blue",
        bold=True,
    )
    click.secho("AutoDL 抢购脚本", fg="bright_blue")
    click.secho("小子！用的时候悠着点，小心号没咯！", fg="bright_red")
    print()
    main()
