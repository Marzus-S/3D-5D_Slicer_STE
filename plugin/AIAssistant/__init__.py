from . import AIAssistant


def getMetaData():
    return {}


def register(app):
    return {"extension": AIAssistant.AIAssistant()}
