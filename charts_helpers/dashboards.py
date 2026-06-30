from collections import defaultdict
from charts_helpers.media_monitoring import media_monitoring_charts
from charts_helpers.pr_calculation import calculate_pr_score
from charts_helpers.reputation import reputation_charts
from db_helpers.schema import DASHBOARDS_ENUM
from .default_charts import chart_calculations


def get_dashboards_chart_data(dashboards: list[str], tagged_articles, brand_keywords, competitors, message_keywords, sections_orders=None):
    """
    Get Charts data
    """
    dashboards = [d.lower() for d in dashboards]
    data_for_insight = defaultdict(dict)
    charts_response = defaultdict(dict)

    # =============================================
    # Media Monitoring Dashboards =================
    # =============================================
    media_monitoring_articles = [item for item in tagged_articles if item.get("is_approved_for_monitoring", False)]

    if DASHBOARDS_ENUM.media_monitoring in dashboards:
        charts_data, grouped_articles = media_monitoring_charts(media_monitoring_articles, sections_orders)

        charts_response[DASHBOARDS_ENUM.media_monitoring] = [charts_data]
        data_for_insight[DASHBOARDS_ENUM.media_monitoring] = {
            "media_monitoring": grouped_articles
        }
    
    # =============================================
    # All Other Dashboards ========================
    # =============================================
    tagged_articles = [item for item in tagged_articles if item.get("is_approved", False)]

    total_count = chart_calculations.total_count(tagged_articles)
    total_reach = chart_calculations.total_reach(tagged_articles)

    if DASHBOARDS_ENUM.media_measurement in dashboards:
        sentiment_distribution = chart_calculations.sentiment_distribution(tagged_articles, len(tagged_articles))
        datewise_coverage = chart_calculations.datewise_coverage(tagged_articles)
        original_vs_syndicated = chart_calculations.original_vs_syndicated(tagged_articles)
        theme_distribution = chart_calculations.theme_distribution(tagged_articles)
        top_publications = chart_calculations.top_publications(tagged_articles)
        publication_reach_sentiment = chart_calculations.publication_reach_sentiment(tagged_articles)
        publish_time_heatmap = chart_calculations.publish_time_heatmap(tagged_articles)
        top_authors_by_publications = chart_calculations.top_authors_by_publications(tagged_articles)
        top_articles_by_sentiment = chart_calculations.top_articles_by_sentiment(tagged_articles)
        
        charts_response[DASHBOARDS_ENUM.media_measurement] = [
            total_count,
            total_reach,
            sentiment_distribution,
            datewise_coverage,
            original_vs_syndicated,
            theme_distribution,
            top_publications,
            publication_reach_sentiment,
            publish_time_heatmap,
            top_authors_by_publications,
            top_articles_by_sentiment
        ]
        
        data_for_insight[DASHBOARDS_ENUM.media_measurement] = {
            "sentiment_distribution": sentiment_distribution.data,
            "datewise_coverage": datewise_coverage.data,
            "original_vs_syndicated": original_vs_syndicated.data,
            "theme_distribution": theme_distribution.data,
            "top_publications": top_publications.data,
            "publication_reach_sentiment": publication_reach_sentiment.data,
            "publish_time_heatmap": publish_time_heatmap.data,
            "top_authors_by_publications": top_authors_by_publications.data,
        }
        
    if DASHBOARDS_ENUM.pr_impact in dashboards:
        sov, competitive_matrix, main_brand, top_competitors = chart_calculations.sov_and_competitve_matrix(tagged_articles)
        sentiment_distribution = chart_calculations.sentiment_distribution(tagged_articles, len(tagged_articles))
        datewise_coverage = chart_calculations.datewise_coverage(tagged_articles)
        publication_tier = chart_calculations.publication_tier(tagged_articles)
        pr_impact = calculate_pr_score(tagged_articles, main_brand, weightage=None, chart_id="pr_impact")
        if competitors:
            pr_impact_competitors = [calculate_pr_score(tagged_articles, cp, weightage=None, chart_id="pr_impact_competitors") for cp in competitors]
            final_pr_impact_competitors = pr_impact_competitors[0]
            final_pr_impact_competitors.data = [pr.data for pr in pr_impact_competitors]
        else:
            pr_impact_competitors = [calculate_pr_score(tagged_articles, cp, weightage=None, chart_id="pr_impact_competitors") for cp, _ in top_competitors]
            final_pr_impact_competitors = pr_impact_competitors[0]
            final_pr_impact_competitors.data = [pr.data for pr in pr_impact_competitors]

        charts_response[DASHBOARDS_ENUM.pr_impact] = [
            total_count,
            total_reach,
            sov,
            sentiment_distribution,
            competitive_matrix,
            datewise_coverage,
            publication_tier,
            pr_impact,
            final_pr_impact_competitors
        ]

        data_for_insight[DASHBOARDS_ENUM.pr_impact] = {
            "share_of_voice": sov,
            "competitive_matrix": competitive_matrix,
            "publication_tier": publication_tier,
            "pr_impact": pr_impact,
            "pr_impact_competitors": final_pr_impact_competitors,
        }

    if DASHBOARDS_ENUM.narrative_intelligence in dashboards:
        message_consistency, coverage_message_consistency = chart_calculations.message_consistency_by_competitors(tagged_articles, brand_keywords, competitors, message_keywords)
        media_types_by_competitors = chart_calculations.media_types_by_competitors(tagged_articles, competitors)
        coverage_overtime_by_competitors = chart_calculations.coverage_overtime_by_competitors(tagged_articles, competitors)
        sentiment_breakdown_by_competitors = chart_calculations.sentiment_breakdown_by_competitors(tagged_articles, competitors)
        publication_by_brands_and_competitors = chart_calculations.publication_by_brands_and_competitors(tagged_articles, brand_keywords, competitors)

        charts_response[DASHBOARDS_ENUM.narrative_intelligence] = [
            total_count,
            total_reach,
            media_types_by_competitors,
            coverage_overtime_by_competitors,
            sentiment_breakdown_by_competitors,
            message_consistency,
            coverage_message_consistency,
            publication_by_brands_and_competitors
        ]

        data_for_insight[DASHBOARDS_ENUM.narrative_intelligence] = {
            "media_types_by_competitors": media_types_by_competitors.data,
            "coverage_overtime_by_competitors": coverage_overtime_by_competitors.data,
            "sentiment_breakdown_by_competitors": sentiment_breakdown_by_competitors.data,
            "message_consistency": message_consistency.data,
            "coverage_message_consistency": coverage_message_consistency.data,
            "publication_by_brands_and_competitors": publication_by_brands_and_competitors.data,
        }

    if DASHBOARDS_ENUM.reputation_index in dashboards:
        current, previous = reputation_charts.current_previous_dates(tagged_articles)

        ri_gauge = reputation_charts.ri_gauge(current, previous)
        ri_timeseries = reputation_charts.ri_timeseries(tagged_articles)
        ri_decomposition_coverage = reputation_charts.ri_decomposition_coverage(tagged_articles)
        pillar_radar = reputation_charts.pillar_radar(current, previous)
        pillar_bar = reputation_charts.pillar_bar(current)
        trust_kpi_breakdown = reputation_charts.trust_kpi_breakdown(current)
        trust_waterfall = reputation_charts.trust_waterfall(current)
        sentiment_coverage = reputation_charts.sentiment_coverage(tagged_articles)
        net_sentiment_coverage = reputation_charts.net_sentiment_coverage(tagged_articles)
        coverage_volume = reputation_charts.coverage_volume(tagged_articles)
        tier1_share = reputation_charts.tier1_share(tagged_articles)
        source_treemap = reputation_charts.source_treemap(tagged_articles)
        theme_volume = reputation_charts.theme_volume(tagged_articles)
        theme_pillar_heatmap = reputation_charts.theme_pillar_heatmap(tagged_articles)
        risk_negative_coverage = reputation_charts.risk_negative_coverage(tagged_articles)
        pillar_small_multiples = reputation_charts.pillar_small_multiples(tagged_articles)
        weight_sensitivity = reputation_charts.weight_sensitivity(current)

        charts_response[DASHBOARDS_ENUM.reputation_index] = [
            ri_gauge,
            ri_timeseries,
            ri_decomposition_coverage,
            pillar_radar,
            pillar_bar,
            trust_kpi_breakdown,
            trust_waterfall,
            sentiment_coverage,
            net_sentiment_coverage,
            coverage_volume,
            tier1_share,
            source_treemap,
            theme_volume,
            theme_pillar_heatmap,
            risk_negative_coverage,
            pillar_small_multiples,
            weight_sensitivity
        ]

        data_for_insight[DASHBOARDS_ENUM.reputation_index] = {
            "ri_gauge": ri_gauge.data,
            "ri_timeseries": ri_timeseries.data,
            "ri_decomposition_coverage": ri_decomposition_coverage.data,
            "pillar_radar": pillar_radar.data,
            "pillar_bar": pillar_bar.data,
            "trust_kpis": trust_kpi_breakdown.data,
            "trust_waterfall": trust_waterfall.data,
            "sentiment_coverage": sentiment_coverage.data,
            "net_sentiment_coverage": net_sentiment_coverage.data,
            "coverage_volume": coverage_volume.data,
            "tier1_share": tier1_share.data,
            "source_treemap": source_treemap.data,
            "theme_volume": theme_volume.data,
            "theme_pillar_heatmap": theme_pillar_heatmap.data,
            "risk_negative_coverage": risk_negative_coverage.data,
            "pillar_small_multiples": pillar_small_multiples.data,
            "weight_sensitivity": weight_sensitivity.data,
        }

    return charts_response, data_for_insight