---
title: "Agentic but Deterministic: Structuring a Product-Led Consulting Project"
date: 2026-07-01
tag: AI Consulting
subtitle: "The most robust AI methodology is not the one that uses the most tokens, but the one that creates autonomous software that can run independently of AI afterwards."
image: /assets/images/consulting_model_diagram.png
description: "The risk of running research projects on AI alone is not only hallucinations — it is that outputs are not repeatable. Here is how to structure a consulting project that is agentic but deterministic."
---

In the summer of 2026 I attended the yearly event organised by a survey platform I am familiar with since before the advent of LLMs. I understood their functionalities from before the advent of the current agentic era. I was selected to attend a meeting on synthetic data (weaving LLM-powered data in the survey research process). They had built a solution that was rather elegant. They embedded LLMs into their processes to help extrapolate what people would have answered to a specific follow up based on their specific profile determined by the options selected either in a survey or in a panel. In the same way that you could extrapolate the optimal price in a conjoint analysis or model a stock price, you could now model what people would answer to a follow up. This solution was elegant because it was embedded. A purely synthetic approach (e.g. pick people in a panel and create 500 surveys guessed from LLMs) would have created a complete hallucination. 

A few weeks after that event, the head of Claude Code, said we had moved to a phase of loop writing. Not only were we developing agents, we had to move towards ["loop engineering"](https://addyosmani.com/blog/loop-engineering/). In that environment the question of how to implement real repeatable solutions that can be validated and can be checked for compliance becomes all the more pressing. **The way forward is to use LLMs in three main ways. First to structure the early research to locate data sources. Then to create software that can analyse those data sources and output the deliverable based on those sources. Finally to link the software if there are any interdependencies, to make sure that findings in one connect to findings in the other**. The more robust AI methodology is not the one that uses the most tokens, but the one that creates autonomous software that can run independently of AI afterwards. 

> The more robust AI methodology is not the one that uses the most tokens, but the one that creates autonomous software that can run independently of AI afterwards.

The risk of running all of your research projects on AI alone is not only hallucinations, it is that they are not repeatable. **The prompt-based workflows that power most AI consultancy today are model-dependent.** If you change the model or the prompt slightly, the output changes with it. Currently, many practitioners inject resources into a folder that works with a specific model, but then try to replicate the folder instructions and skills with a new model and their whole output looks different. There are places where the lateral probabilistic thinking of AI is precious, there are others where you still need deterministic output at scale. LLMs give you the ability to create solutions at scale, but you need to know when to implement deterministic architectures and when to integrate probabilistic ones. This is an example of how to differentiate between them. 

> LLMs give you the ability to create solutions at scale, but you need to know when to implement deterministic architectures and when to integrate probabilistic ones.

## Building the data architecture

The way to structure a project in consulting these days is to prompt agents to create software. This can be done in SQL, python, R, Cobol, any language will work. This layer anchors the process and makes it auditable and compliant. It can also be updated at virtually no cost. 

A normal consulting project is much more based on the available data, than on the theoretical questioning behind it. Whereas a pyramidal consulting project would have set up the research project ahead of the research, the initial discovery is shorter now and the process needs to be much more bottom up starting from the data rather than the hypothesis. You still need a structured thought process to create the layers of software (i.e. a methodology that will help you analyse the data). To give a practical example, a client in the education service sector asked for help understanding whether they were having better impact than their competitors (a benchmarking exercise) and how they could improve that impact. 

## Embedding the deliverable in the analysis

Traditionally, I would have created a triangulation approach, collecting as much information as possible and finding the information in the middle. The best way of approaching the problem now, was building a sophisticated analytical pipeline that could provide a much more robust answer. Triangulation was a method of verification, today, the methods available are much more solid and don’t require the guess work. I was able to collate up to 7 disparate data sources (with unique identifiers) and was able to create a multi-year performance analysis with rigorous regression testing and an evaluation of the key variables driving performance. That was done with a single codebase. The code also output the graphs so the narrative was embedded in the data analysis. The output was reproducible, auditable, and entirely traceable back to its sources, the code was the methodology.

## Modelling at scale: survey, weighting, cluster analysis

In a second phase I wanted to understand why their end users were performing better and so conducted a survey representative of the UK population. I tracked the quotas with a python code and was able to track in real time which populations were underrepresented and overrepresented. 

I created a second script to weight and analyse the data. This script also created the file output that was needed and I was able to add this into the presentation. Finally I created a further script built on top of that first survey analysis to perform a cluster analysis.

In the end **I was able to produce a strategy game plan with over 100 possibilities based on the personas and the customer status**. That means that the client doesn’t have to believe my narrative. They can apply the logic themselves ahead of a client engagement call. They can now predict the preferences of a client based on their characteristics. The deliverable became a universal sales and engagement playbook that follows clients from first contact until they are mature. 

> The client doesn’t have to believe my narrative. They can apply the logic themselves ahead of a client engagement call. They can now predict the preferences of a client based on their characteristics.

## Linking it all together: layered coding

The way to build a consulting project is to use agentic capabilities to construct layers of software. This new reality is structurally sounder than the VBA-based Excel work of the past. It links the data to output production and does so in a more streamlined way. Just as workstream owners had to align their findings manually in the old model, scripts can now share data automatically via JSON or CSV files. Results on one stream can thus be reflected on another stream. A new project begins by deploying a master script that knows your template and breaks each question into specific automated workflows. Each of the analyses for those pieces of data is run by a script that produces the output for that specific element. 

Deliverables are not limited to PPT and Excel. For instance you get much neater results with HTML dashboards embedded in your website. This will change the client journey. They will expect access to the code, not just the output. They will want to interrogate the data themselves. The creation of ad-hoc databases per project with layers of connected coding will inevitably increase the transparency of the delivery process and will require much more sophisticated methodologies. 

The relationship with the client will also change as a result. The clients will get used to more interactive visualisations, and will probe much more on how the data analysis architecture is structured. The use of LLMs has created a needed fear for hallucinations in the analysis process, and in the same way that quoting your sources is now mandatory, showing how the analysis was reached is a non-negotiable. **That accountability will irk a fair few old-style players but being able to justify the process is what will justify the premium in the future.**
