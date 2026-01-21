# plan
if you have any questions, try to ask them in a way a non technical PM can understand

we will use test driven development:
0. if you think the bug will require feature tests - write "NOT TOUCHING THIS" and say why
1. create a short and concise human-readable spec document:  current flow /  intended flow
2. now let's start solving the bug
3. we will do spec driven development:
4. write a spec for the bug 
5. see it fails 
6. write code to make the spec pass 
7. run it 
8. if it fails, change the code (not the spec) to make it run again 
9. do so until the spec passes, if you feel you're stuck in a loop - write STUCK and specify the reason 
10. tell me which relevant specs i should run to see you didnt break anything 
11. after implementing the fix, review yourself using the instructions from instructions file 
12. then run rubocop checks - if the changes we introduced caused offenses, fix only those offenses 
13. recommend a good commit message
