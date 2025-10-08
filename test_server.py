from summoner.server import SummonerServer
from summoner.your_package import hello_summoner

if __name__ == "__main__":
    hello_summoner()
    SummonerServer(name="test_Server").run(config_path="test_server_config.json")
