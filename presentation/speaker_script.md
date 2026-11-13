# Speaker script: "Does the Cache Forget?" (about 15 minutes, 23 slides)

There are two speakers, about 7.5 minutes each. A = Sabuhi (theory), B = Ilkin (project and results). Swap the names if you prefer. The hand-over happens at slide 12. Full notes for every slide are in the deck's speaker notes.

| # | Slide | Who | Time | Key line to say |
|---|---|---|---|---|
| 1 | Title | A | 0:30 | "We teach a bit of theory, then show what a small model loses when we save only the big model's top guesses." |
| 2 | Big models are expensive | A | 0:35 | "Teacher 1.71 B parameters, student 135 M, 12.7× fewer." |
| 3 | Knowledge distillation | A | 0:35 | "Blue is always the teacher and orange is always the student. The student copies the teacher's guesses." |
| 4 | Soft guesses | A | 0:45 | "The label says only 'mat'. The teacher also says floor and sofa are fine. That is dark knowledge." |
| 5 | KL training signal | A | 0:45 | "One number, KL(teacher‖student), measures how different the bars are. Our main results use forward KL." |
| 6 | Storage problem | A | 0:40 | "Storing everything takes 171 GB. Our cache is 1.07 GB and was built in 6.4 minutes." |
| 7 | Top-k caching | A | 0:40 | "We keep the top 8 and throw away the tail. That tail is still 9.8% of the probability." |
| 8 | Two fixes | A | 0:45 | "A ghost token stores the tail as one lump. RS-KD stores 50 random draws instead." |
| 9 | Popular but safe? | A | 0:30 | "It is cheap and standard, but the student never sees the tail." |
| 10 | Research question | A | 0:20 | "What does top-k do to what the student actually writes?" |
| 11 | Teacher vs student | A | 0:35 | "The student is deeper (30 vs 24 layers) but much narrower (576 vs 2,048)." |
| 12 | The experiment | **B** | 0:40 | "Six teacher signals, three seeds each. Everything else is identical, and every model is tested on 500 test prompts." |
| 13 | How we measure | B | 0:45 | "ROUGE-L for quality, plus diversity, Self-BLEU, honest confidence (ECE) and rare words." |
| 14 | Greedy example | B | 0:30 | "With greedy decoding the top-8 and full-KD answers are almost the same, and both are correct." |
| 15 | Diversity example | B | 0:40 | "One example, not the evidence. Top-8 names 3 cities in 8 tries, full KD names 4, and both make mistakes." |
| 16 | Dose-response | B | 0:50 | "Top-8 is 14% less diverse and has 1.9× the calibration error, yet greedy ROUGE-L is unchanged." |
| 17 | Temperature | B | 0:55 | "Warm top-8 up and it lies on full KD's line. Our pre-registered test was formally inconclusive." |
| 18 | Amount vs identity | B | 0:50 | "The ghost token closes 75% of the gap in amount but only about half in identity. RS-KD closes 93–98%." |
| 19 | Summary | B | 0:40 | "Top-k makes students sharper and forgets which tail words matter. If you cache, use random samples." |
| 20 | Hardware | B | 0:30 | "One RTX 4060 laptop GPU, 66 runs, 53.4 GPU-hours of training." |
| 21 | Limitations | B | 0:30 | "One model family, one dataset, small models, automatic metrics. AI assistants helped with code and drafting, and we checked everything." |
| 22 | Next steps | B | 0:20 | "A cheaper cache that keeps tail identity, a second model family, ACL 2027." |
| 23 | Thank you | A+B | 0:10 | "Questions?" |

The total is about 14.5 minutes, which leaves some buffer.

**Likely questions (whoever is closer to the topic answers)**

- **"Why is the result inconclusive if top-8 is less diverse?"** At temperature 1 it is less diverse. When both students have equal quality, it is slightly more diverse. Our pre-registered rule only allowed two outcomes, "deficit" or "equal within ±0.010", so a result above the margin counts as inconclusive. (B)
- **"Why not simply use full KD?"** Full-KD runs took a median of 81 minutes against 40 minutes from the cache. Storing the full teacher output would take 171 GB. (A)
- **"Is RS-KD more expensive?"** It stores 50 draws per position, which comes to about 8.7 distinct words on average. That is still a small file compared with the full output. (A)
