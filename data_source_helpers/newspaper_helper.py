from newspaper import Article


def article_content_fetch(article):
    try:
        fetched_article = Article(article["url"])
        fetched_article.download()
        fetched_article.parse()
        json_article = {
            "title": fetched_article.title,
            "url": fetched_article.url,
            "content": fetched_article.text,
            "date": str(fetched_article.publish_date or fetched_article.meta_data.get("iso-8601-publish-date", None) or article["published"] or ""),
            "author": ", ".join(fetched_article.authors) if fetched_article.authors else ""
        }
        return json_article
    except:
        return {
            "title": article["title"],
            "url": article["url"],
            "content": "Subscription",
            "date": article["published"],
            "author": ""
        }